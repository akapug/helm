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
import functools
import io
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import chat, codexhomes, home, hooks, human, injection_config, meld, pk, seats, todos, web  # noqa: E402,E501

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
    "ownership_census.py": (
        "LAUNDERED via _seat_label, AND IT IS AN EMISSION SITE. One site, "
        "`roster_checked()` in `live_inputs`, which reads last_seen for every "
        "rostered seat so `owner_state` can judge whether a row's owner is "
        "working. TWO things derived from that read leave the module, both "
        "through the single `_label` door in this file: the THRESHOLD "
        "TRIPWIRE lines, which are the only place ROSTER KEYS are rendered "
        "(gap_seats carries them), and the per-ledger dark-seat lines, whose "
        "names are LEDGER-sourced (a dispatch `recipient`, a task `owner`) "
        "and laundered anyway because they are free text too. The verb prints "
        "to an operator's terminal and the tripwire is the most prominent "
        "column in it, so a roster key carrying ESC or bidi would reshape the "
        "one surface an operator is meant to trust when deciding whether a "
        "seat is dead. `_seat_label` leaves a legitimate seat BYTE-IDENTICAL, "
        "so nothing is lost. ONE DOOR RATHER THAN PER-FIELD: laundering at "
        "each format site is a bet re-placed at every branch, and the next "
        "name added to the renderer is the one that forgets. NOT WIRED INTO "
        "THE SECTION-B SWEEP: that sweep drives named verbs against a table; "
        "the arm standing in for it lives beside the module in "
        "tests/test_ownership_census.py and asserts a hostile key loses its "
        "control bytes while a legitimate seat survives unchanged."),
    "fixedtext.py": (
        "LAUNDERED via _seat_label, AND IT IS AN EMISSION SITE. One site, "
        "`roster_checked()` in `live_rows`, read for each row's recorded home "
        "so a checkout can be listed with the seats that share it. ROSTER "
        "KEYS leave this module at three doors and every one passes through "
        "`_seat_label` first: the doctor line in `findings`, the report in "
        "`cmd`, and the `--json` rows, whose `seats` list is rebuilt from the "
        "laundered names rather than dumped from the survey. The checkout "
        "path printed beside them is the row's `cwd`: `checkouts` admits it "
        "only when it is printable text, so a home carrying a control "
        "character is never rendered."),
    "seats_integrator.py": (
        "LAUNDERED via _seat_label, AND IT IS AN EMISSION SITE. One site, "
        "`roster_checked()` in `integrator_seat`, which resolves WHO THE "
        "INTEGRATOR IS: a direct key lookup, then an inference over rows "
        "whose key ends in the integrator suffix. Two things leave this "
        "module and BOTH launder. (1) The REFUSAL text names the seat asked "
        "for and, on ambiguity, every candidate — every one through "
        "`_seat_label` before it reaches the string. (2) `integrator_mention` "
        "returns an `@name` prefix that is posted to a ROOM, which is a "
        "terminal: it launders too, and that is safe rather than lossy "
        "because `_seat_label` leaves a legitimate seat BYTE-IDENTICAL, so "
        "the mention still resolves, while a key carrying ESC or bidi loses "
        "it and correctly fails to resolve. NOT WIRED INTO THE SECTION-B "
        "SWEEP: that sweep drives named verbs and endpoints rather than a "
        "table, so adding this module is a driver rather than a row, and it "
        "is not written. The arm that stands in for it lives beside the "
        "module, in tests/test_seats_integrator.py, and asserts a hostile "
        "roster key loses its control bytes in both emissions."),
    "landreq.py": (
        "LAUNDERED: administrative retirement reads the roster to answer ONE "
        "question — is any party on this land request still reachable, so the "
        "row must NOT be retired out from under a live seat. TWO sites, both "
        "`seats.roster_checked()`: seat_reach's own fallback, and the single "
        "`_reach_probe_roster` that BOTH the retire door and the sweep bundle "
        "call — they used to hold one inline read each, which is exactly how "
        "the two drifted apart. seat_reach is the only one that RENDERS a roster key — its "
        "LIVE/DARK detail names the matched row — and both that key and the "
        "session id it prints go through seats_common._seat_label first. "
        "Everything else these emissions carry (`row[\'id\']`, the party "
        "name, the role) is LEDGER-sourced, not roster-sourced. The refusal "
        "string reaches an operator's terminal, so this module is an emission "
        "site and is allowlisted as one, not as a membership reader."),
    "turnstamp.py": (
        "RESOLUTION-ONLY, AND IT RENDERS NOTHING THE ROSTER SUPPLIED. ONE "
        "read, `seats.roster_checked()` inside `reconcile`, which exists to "
        "answer a single question before a record is written: does the seat "
        "name this process inherited from its environment still own the "
        "session this hook payload carries? A record filed under the wrong "
        "seat is another seat's liveness wearing this one's name, so the "
        "binding is reconciled rather than asserted. What LEAVES is a "
        "canonical key that becomes a hashed filename through "
        "`seats_common._seat_key` — never a terminal — and refusal strings "
        "that are FIXED PROSE. The ambiguity refusal deliberately does NOT "
        "pass `canonical_seat`'s own message through, because that message "
        "interpolates the roster's raw key spellings; re-emitting it here "
        "would have made this module a rendering site for hostile roster "
        "content on a path nobody was watching. It is allowlisted as a "
        "membership reader, NOT as an emission site."),
    "seat_reassign.py": (
        "RESOLUTION-ONLY, AND THE NAMES IT BINDS ARE ALREADY PUBLIC. `helm "
        "seat reassign` reads the roster to answer three EXACT-KEY questions "
        "and nothing else: does this source token name a seat, does this "
        "target, and which family members exist so an ambiguity can be "
        "REPORTED rather than broken silently. The verb's whole job is to "
        "hand holdings from one roster key to another, so the keys are its "
        "operands — they arrive from the operator's own command line and "
        "leave in a manifest naming the two seats the operator just named. "
        "Nothing derived from the roster is rendered that the operator did "
        "not already type, and no third seat's name is printed. The seat "
        "spelling is deliberately taken VERBATIM from the roster rather than "
        "casefolded, because `_binding_ok` compares holders raw and a "
        "laundered spelling would hand a lease its holder cannot use."),
    "work/_guard.py": (
        "INTERNAL-MATCHING-ONLY: the guard installer reads the roster once to "
        "ask whether the seat-name rung's authority file still covers every "
        "live seat — a MEMBERSHIP question, because the rung itself reads a "
        "file and a file goes stale silently. What leaves is a COUNT and the "
        "authority's path; the names are deliberately NOT printed (`helm chat "
        "seats --all` is the reader for those). An install summary that "
        "listed roster keys would be an emission site, and the rung it "
        "configures exists precisely to keep seat identities out of places "
        "they do not belong."),
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
        "_pub_row and _ROW_CAPS bound what else escapes. "
        "Relocated verbatim from seats.py by the split — this entry is the "
        "reviewer contract for the surface, not a formality."),
    "chatdebris.py": (
        "INTERNAL-MATCHING-ONLY: `unpaired_session_cursors` takes ONE "
        "roster_checked() snapshot to learn which seat each live session is "
        "bound to, and uses the roster KEYS only as inputs to _seat_key, an "
        "equality set it compares cursor FILENAME keys against. It returns "
        "chat-dir paths the gc row prunes and prints as `pruned <path>`; the "
        "path is the cursor file's own name, which the seat-key hash already "
        "made, never a roster key or a display field. An unreadable roster "
        "raises a fixed sentence."),
    "seats_gc.py": (
        "INTERNAL-MATCHING-ONLY: the roster GC reads the roster to decide "
        "which rows are reapable and returns [{seat, verdict, why}] — the "
        "seat KEY raw, and `why` interpolates that same key (\"carries "
        "HELM_CHAT_NAME=%s\"). NEITHER IS EMITTED HERE. Its one consumer, "
        "the `helm chat seat gc` leg in seats_cli, launders both columns at "
        "the print site (_seat_label on the key, _scrub+_clip on the why) "
        "and that is where the emission contract lives. The apply path's "
        "second read is roster_for_write, the fail-CLOSED door, not this "
        "one. Split out of seats_report at 999/1000 lines; the sentence "
        "about the GC that used to ride that module's entry is this one."),
    "seats_receipts.py": (
        "LAUNDERED: _broadcast_recipients iterates roster KEYS to build the "
        "sender's broadcast census. Raw keys drive only identity, delivery, "
        "mute, wake, and sink calculations; render_delivery_receipts passes "
        "every emitted seat name through _seat_label at the single sink."),
    "seats_ack.py": (
        "INTERNAL-MATCHING-ONLY: the ack ladder reads the roster to decide "
        "whether a row's recipient IS a seat and which lanes it should scan "
        "— membership questions about names the caller already holds. What "
        "leaves is a per-(recipient, row) state and a count. Relocated "
        "verbatim from seats.py by the split."),
    "seats_claims.py": (
        "INTERNAL-MATCHING-ONLY: claim_holder_listedness reads the roster to "
        "ask whether a claim's HOLDER has a row at all — a membership "
        "question about a name the caller already holds, iterated only "
        "through recipient_matches. What leaves is one of three fixed words "
        "(listed / unlisted / unknown) and, from claim_unlisted_mark, a "
        "constant sentence; NO roster key appears in either return value. "
        "The holder name is rendered by the CALLER, and the roster surface "
        "launders it through _seat_label before printing the gap summary."),
    # THE KEY MOVED WITH THE CODE, and the reason is unchanged because it was
    # always about the CLAIMS RUNG rather than about the file that happened to
    # hold it. seats_stop_guard.py is GONE from this allowlist: it now reads
    # the roster ZERO times, and an entry for a module that no longer consumes
    # would rot into a vouch for a call site that does not exist.
    "seats_stop_claims.py": (
        "INTERNAL-MATCHING-ONLY: the claims rung takes one tri-state roster "
        "snapshot to resolve session ownership and distinguish a foreign live "
        "holder from an unlisted alias. Roster keys drive only matching and "
        "latch identity; any holder rendered in a verdict comes from the claim "
        "row, while ambiguous roster names are laundered by seats_roster's "
        "canonical resolver before its warning."),
    "stopfacts_resident.py": (
        "INTERNAL-MATCHING-ONLY: the stop-facts resident takes one tri-state "
        "roster snapshot per refresh to choose WHICH seats to compute review "
        "rounds for and to index a claim's minting session to its seat. Roster "
        "keys are used as lookup keys in the snapshot file the Stop hook reads; "
        "the hook looks facts up by the seat it already resolved and never "
        "prints a key it read from the file."),
    "seats_stop_signals.py": (
        "INTERNAL-MATCHING-ONLY: the stop ladder reads the roster once, to "
        "ask whether THIS seat's own row is present and armed before it "
        "decides whether an idle stop is allowed. It is a membership "
        "question about a name the process already holds; nothing keyed by "
        "roster leaves. Relocated verbatim from seats.py by the split."),
    "seats_cursor.py": (
        "INTERNAL-MATCHING-ONLY: restore_cursor_sweep derives hashed paths; "
        "seat_incarnation matches a keyed writer to one durable roster "
        "generation. Both return only state/counts; no roster key reaches a "
        "display sink."),
    "seats_delivery.py": (
        "INTERNAL-MATCHING-ONLY: the delivery path reads the roster to decide "
        "WHO A ROW REACHES — resolve_recipient, recipient_capability, "
        "_deliver_unpaused — and the keys it reads are compared, never "
        "emitted. What leaves is the row text and a cursor position; the one "
        "place a seat name is rendered for a human is _seat_label, which "
        "launders. Relocated verbatim from seats.py by the split."),
    "seats_mute.py": (
        "THE WAKE FILTER'S OWN MODULE, extracted from seats_roster because a "
        "mute is not a fact about the roster ROW. Its single read is "
        "`mutes()`, which returns the row's OWN mute list and never the key "
        "it looked the row up by, so no roster key leaves this module as "
        "display text at all. The writer beside it, `set_mute`, resolves its "
        "token through the roster's own `_resolve_seat` and writes the key "
        "back as a dict key, which is the same non-emitting shape the "
        "roster's module is allowlisted for."),
    "seats_roster.py": (
        "THE ROSTER'S OWN MODULE, relocated by the seats.py split. It is the "
        "writer as well as the reader, so its keys never leave as display "
        "text — the CLI table that DOES emit them is roster_report, which "
        "stays in the report section and carries its own entry. "
        "AMENDED task/910: this used to say the reads here are the ones that "
        "WRITE a row and run the identity-admin verbs. That is no longer "
        "true and leaving it would have described five call sites that moved "
        "— every whole-file write now acquires through "
        "seats_common.roster_for_write, which does its OWN single read and so "
        "is not a roster() consumer at all. The three roster getter calls left "
        "in this module are PURE READERS: roster_checked narrows one acquired "
        "snapshot, while seats_for_session() and mutes() read for lookup. They "
        "are named from the AST rather than guessed, and none was ever part of "
        "the write hazard."),
    "seats_lineage.py": (
        "THE SITE MOVED AND THE TOTAL IS CONSERVED — seat_launch_assets 1 -> "
        "0, here 0 -> 1 — but the CONSUMER SET is not, and that half is what "
        "needs reviewing. The walk arrives verbatim from the launcher; what "
        "is new is that the land-request board now reaches the roster through "
        "it, to answer a question nothing asked before: "
        "given a seat NAME an open row still spells, does a live row answer "
        "to it, does the rename lineage name a successor, or does nothing "
        "answer at all. One roster_checked() snapshot answers all three, and "
        "roster_checked rather than roster() is the point — its "
        "all-or-nothing contract means a roster this reader cannot validate "
        "returns UNKNOWN, where the fail-open reader would let one malformed "
        "row turn a live seat into an ORPHANED banner on the board. THE KEYS "
        "IT EMITS ARE LAUNDERED AT THE BOUNDARY: the successor name and the "
        "roster's own spelling come back from rows this reader validated, and "
        "the one string it did NOT get from the roster — the dead name an lr "
        "row has carried since it was written — goes through _seat_label "
        "before any caller prints it."),
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
        "INTERNAL-MATCHING-ONLY: `seat_reading` (behind `derived_seat` and "
        "`signing_identity`) reads the roster STRICTLY to answer ONE "
        "boolean about a name it already holds — is this row a FLEET ACTOR "
        "(home_room set) rather than an observed session? The name comes from "
        "`meld._self_seat`, which resolves a live rename alias to the renamed "
        "row's key (task/3049), so it re-passes `home.validate_seat_arg` "
        "before it can leave; the read is `.get(name)`, a membership lookup "
        "whose miss returns \"\". `_session_dispute`, asked only where the "
        "owner's profile would stand, reads which rows bind this process's "
        "session, and the one key it can return is laundered through "
        "`seats_common._seat_label`. It emits nothing itself — its caller "
        "uses the answer to decide whether to SIGN or refuse."),
    "chat.py": (
        "LAUNDERED via seats_common._seat_label at the seam in "
        "_live_same_family (the spawn steer, via _spawn_roster): the ONE "
        "emitting site, and it emits the key inside a PASTEABLE command "
        "(helm chat dm <seat>) on the argv-guard stderr, so a sink-side "
        "launder would print a wrong command. A key is named only when it "
        "survives _seat_label unchanged AND matches dispatches._TOKEN; any "
        "other key is dropped from the candidate list and never named or "
        "counted. Verified by tests/test_chat_spawn_steer.py "
        "test_a_seat_name_that_cannot_be_named_inertly_is_not_named_at_all "
        "(forged steer line, ESC+bidi, shell metacharacters, whitespace, "
        "empty). The TREE rung (tree_steers, via _tree_roster) is the second "
        "emitting site and keeps the same law at the same kind of seam: it "
        "names a roster key, and that row's recorded home room, inside a "
        "pasteable command on the argv-guard's advisory envelope, each only "
        "when it is inert as written (_seat_label-unchanged AND "
        "dispatches._TOKEN for the key, _TOKEN for the room, which otherwise "
        "falls back to the validated project name). Verified by "
        "tests/test_chat_tree_steer.py "
        "test_a_name_that_cannot_be_named_inertly_is_not_named."),
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
        "of the census. It was RAW until 2026-08-04: fleet.py held zero "
        "scrub/label calls, and the regex tripwire could not see it because "
        "fleet obtains the roster through roster_checked()."),
    "orcaadopt.py": (
        "TWO SITES, DIFFERENT REASONS. (a) INTERNAL-MATCHING-ONLY: "
        "`roster.get(seat)` reads the row's session and sessions SIDS for pane "
        "adoption; the seat key is the caller's own argument, used to index. "
        "(b) LAUNDERED-BEFORE-EMIT as of 2026-08-04: `roster_current_sids()` "
        "reads roster KEYS, and the session join hands the winning key out as "
        "a pane row's `seat` — which `seat._panes` prints in its first and "
        "widest column. That key is now scrubbed at the sink through "
        "`seats._seat_label`, the same launder every roster-borne display "
        "string clears. The old reason for this module — 'no roster key is "
        "emitted' — stopped being true the moment the listing learned the "
        "session join, and this pin is what said so."),
    "proxywatch.py": (
        "THREE INTERNAL-MATCHING-ONLY SITES PLUS ONE THAT EMITS. health() looks "
        "up launch-owned runtime metadata by the watched seat name it already "
        "holds. _roster_session() resolves that caller-owned name and uses "
        "matching keys only to select one exact session. "
        "_roster_identity_for_session() selects one key for an already-held "
        "session and hands it back only as the next internal lookup argument. "
        "THE FOURTH IS _session_keyed_seats(), which enumerates the roster to "
        "key adopted seats by session, and it is not internal-matching-only: "
        "the seats it returns are consumed by MEMBERSHIP at both call sites "
        "(proxywatch's `row['seat'] in live`, which emits its own pre-existing "
        "watched-seat value, and autocompact's holding census), but its "
        "REFUSALS become operator prose in _live_seats's `blind`. That one "
        "emission is laundered at the sink with _seat_label."),
    "reviewer_eligibility.py": (
        "LAUNDERED via _label, AND IT IS AN EMISSION SITE. ONE read, "
        "`seats.roster_checked()` in `_read_roster`, and the ROSTER IS THE "
        "CANDIDATE SET for `helm reviewers <row>`: every key becomes a "
        "candidate seat, so every key can reach this verb's sinks. It has "
        "four of them and all four are roster-sourced — the seat column of "
        "the ELIGIBLE table, the EXCLUDED lines, the reason strings that "
        "interpolate a seat (including a pasteable `helm seat resume <name>` "
        "repair) and the whole `--json` body, whose `seats`, `eligible`, "
        "`measured_eligible` and `out_of_scope` all carry keys. ONE DOOR: "
        "`reviewer_eligibility._label` wraps `seats_common._seat_label`, and "
        "a roster key becomes a report value in exactly one statement (the "
        "candidate row's `seat` field) plus the `out_of_scope` key and the "
        "five reason strings, so no format site launders for itself. The RAW "
        "key still drives every internal match — the tier policy, the "
        "usability join and the pane read each receive it unchanged — which "
        "is the documented seam behaviour: only the EMITTED value is the "
        "hazard. The other names this verb prints (the row's author and "
        "reviewer) are LEDGER-sourced, not roster-sourced. NOT WIRED INTO "
        "THE SECTION-B SWEEP: that sweep drives named verbs against a table "
        "and this verb needs a land-request row plus six injected "
        "authorities, so the arm standing in for it lives beside the module "
        "in tests/test_reviewer_eligibility.py "
        "(HostileRosterKeyNeverReshapesTheTerminal) and asserts a hostile "
        "key loses its control bytes in BOTH the render and the --json body "
        "while a legitimate seat beside it survives byte-identical."),
    "resumeturn.py": (
        "TWO IDENTITY-ONLY READERS. _native_registered uses `seat_name in "
        "roster` membership to decide whether the caller-supplied name is "
        "known. _recovery_owner builds a casefold index so a live/default/quiet "
        "candidate is written and DMed under the checked roster's canonical "
        "addressing token; that token is an exact recipient identity, not a "
        "display field, and roster unreadability yields no assignee. "
        "_deaf_attendance reads one seat's attendance row for the manual "
        "repair door and emits nothing. _roster_row is one strict read that "
        "resolves the canonical key of a seat the caller already names and "
        "returns its row for the keystroke authorization and the attempt "
        "standing; it emits nothing."),
    "todos.py": (
        "LAUNDERED: fleet()/_row run the seat KEY + project through _lbl "
        "(_scrub + _clip) before the fleet table AND the /api/todos JSON; "
        "the todo TEXT is scrubbed in digest()."),
    "codexhomes.py": (
        "LAUNDERED: _print_capacity emits the live codex seats through "
        "seats._seat_label."),
    "beacons.py": (
        "FOUR READERS, AND EXACTLY TWO OF THEM ITERATE. `roll()` binds roster "
        "KEYS while building the census roll, and passes the mapping only to "
        "seats.session_owners — the exact read-only identity index that detects "
        "ambiguous historical sessions. Every admitted name, including one "
        "returned by that index, reaches the report through beacons.label -> "
        "seats._seat_label. `attend()` also binds roster KEYS, for a different "
        "owner-layer reason: a census seat and its enrolled display key are "
        "casefold-identical but may differ in casing after a case-only rename, "
        "so attendance builds the validated casefold->enrolled-key index before "
        "updating the existing row. It filters every bound key through "
        "beacons.valid_seat, never mints a miss, and passes transition names "
        "only to delivery renderers that use beacons.label. This module carries "
        "a SECOND unvalidated source no other consumer has — a seat name read "
        "from ANOTHER PROCESS's `--seat` argv or environ, which passed no join "
        "seam at all — and that one is laundered where it ENTERS the row "
        "(classify), not left to whoever prints it. _ack_alerts and "
        "_record_repairs are deliberately membership-only: each walks rows it "
        "was HANDED and touches the register only as `r.get(seat)`; a miss is "
        "a SKIP and no roster key is bound. escalate binds keys like attend "
        "does, because its pane-repair leg resolves the roster's ENROLLED "
        "spelling for a census seat rather than being handed a resolved name. "
        "BeaconsRosterReaderShapeTest below pins the exact reader, shape, AND "
        "mapping-access contract, so neither an arbitrary new iterator nor a "
        "membership reader that starts binding keys can hide behind this "
        "rationale."),
    "web:_api_chat": (
        "LAUNDERED: the roster @mention list routes through _seat_label and "
        "the sidebar rows through _room_seats/_rooms_summary."),
    "web:_rooms_summary": (
        "LAUNDERED: every room sidebar row is built through _room_seats; the "
        "semantic function key survives the web.py physical split."),
    "web:_dm_seat_map": (
        "INTERNAL-MATCHING + LAUNDERED-at-emit: builds {state-file key: seat} "
        "so DM lanes resolve to their recipient (task/62). The only roster KEY "
        "that leaves is the sidebar `seat` label, laundered via "
        "seats._seat_label at its ONE emit site in _rooms_summary — exercised "
        "by the runtime sweep's planted hostile-seat DM lane. The POST path "
        "consumes the raw name solely as seats.dm's addressee, which "
        "re-validates it as an exact token."),
    "hooks.py": (
        "LAUNDERED: surface_uncovered launders BOTH the printed pane-name "
        "column AND the interpolated reason via seats._seat_label. "
        "unsigned_panes reads the roster only to ask whether a pane's own "
        "declared name is a fleet actor, and launders both names it puts in "
        "its reason."),
    "injection_config.py": (
        "LAUNDERED-BEFORE-EMIT: _roster_row uses raw roster keys only for "
        "casefold and exact-session matching, then passes the one selected key "
        "through seats._seat_label before the Config JSON target can expose it. "
        "The runtime sweep drives the session-only path so the requested "
        "selector cannot hide a raw-key leak."),
    "seats_common.py": (
        "RESOLUTION-ONLY, AND IT IS THE DOOR THE OTHERS STOPPED BEING. Three sites, the same line in `canonical_keys`, `seat_row` and (task/2338) `live_alias`: the roster is read to answer ONE question — which stored key, if any, IS the identity the caller spelled, or is the row that name is a live rename alias of. What leaves is a key the ROSTER already holds or None; a name nobody holds yields the empty list and mints nothing, and a roster holding two casefold-equivalent rows REFUSES with a message that names the variants rather than guessing between them. The refusal is the only emission and it reaches an operator's terminal, so this module is allowlisted as an emission site: the keys it renders there are the stored spellings themselves, which is the whole content of the complaint. This entry EXISTS because four other modules stopped indexing the roster by a caller-supplied name and started asking here instead — seat.py dropped off this allowlist in the same change, and seat_reassign.py and seats_identity.py each shed sites. The consumer did not appear; it MOVED, and the count pin below records both halves."),
    # seat_launch_assets.py IS GONE FROM BOTH DICTS (task/2333), not amended
    # to say it kept a laundering discipline it no longer exercises. Its
    # lineage walk moved to seats_lineage.py and took the module's only roster
    # read with it, and THE ALLOWLIST CANNOT ROT is the arm right beside this
    # one: an entry for a module that no longer calls roster() is a standing
    # exemption nobody can re-derive. _launch_identity still answers the same
    # question with the same discipline — it just asks somebody else now.
    "seat_health.py": (
        "ONE SITE HERE, AND A SECOND READER THIS CENSUS CANNOT SEE. (1) the "
        "counted site is INTERNAL-MATCHING-ONLY: live codex instances via "
        "last_seen, emitting a numeric count and never a stored roster KEY. "
        "(2) NOT COUNTED, AND SAID OUT LOUD RATHER THAN LEFT TO BE "
        "DISCOVERED: `seat list`'s population note reads the roster FILE "
        "directly — pk.read_json(roster_path(), strict=True) — because "
        "seats_common.roster() is the non-strict path and cannot raise, so a "
        "corrupt roster would arrive as {} and print the all-clear the note "
        "exists to refuse. This scan counts roster() CALL SITES, so a direct "
        "path read is invisible to it: the note DOES emit roster keys and "
        "they DO leave through seats_common._seat_label at the single join, "
        "but nothing in this file would catch it if they stopped."),
    "seat_reachability.py": (
        "INTERNAL-MATCHING-ONLY: capture freezes the checked roster as evidence "
        "for exact-token authority classification. Raw keys are compared only "
        "inside the replayable snapshot; results emit the caller-supplied party "
        "token plus fixed LIVE/QUIET/ABSENT/UNKNOWN states, never a roster key."),
    "seat_resume_all.py": (
        "LAUNDERED-at-emit: the post-reboot sweep reads roster KEYS once "
        "(roster_checked) to enumerate orca-ADOPTED seats — rows helm never "
        "spawned — and each becomes the first, widest column of the sweep's "
        "table. Row.line() is the ONE emit site and launders through "
        "seats._seat_label; the key itself is consumed only as "
        "orcaadopt.resolve's exact addressee. Driven by the runtime sweep's "
        "planted hostile seat (test_seat_resume_all_table_is_inert)."),
    "seat_rehome.py": (
        "INTERNAL-MATCHING-ONLY: the register surface of `verify` reads the "
        "roster ONCE (roster_checked) to ask whether the row for the seat "
        "the OPERATOR named still carries the session id the rehome "
        "planned. The key never leaves: canonical_seat narrows the "
        "operator's argv spelling to the roster's own key, that key is "
        "consumed only as the `roster.get` operand, and the ambiguity "
        "branch DISCARDS the sentence canonical_seat renders (it reads as "
        "'not in the chat roster'). What the three register lines "
        "interpolate is the seat name the CALLER typed and the session id "
        "the plan already printed; the one roster FIELD that reaches the "
        "terminal is the row's own `session` in the UNPROVEN mismatch "
        "sentence — a session id, never a roster key, the same class "
        "seats.py's _dispute_sentence renders. NOT wired into the "
        "section-B sweep for the same reason seat_reachability.py and "
        "stalebot.py are not: no roster key can reach a sink here, so the "
        "planted hostile key has nothing to leak through."),
    "seat_usability.py": (
        "INTERNAL-MATCHING-ONLY: the usability join reads the register once "
        "per pass to ask ONE question about a name the caller already holds — "
        "is this seat's runtime VERIFIED, and does it have a register row at "
        "all. It binds no roster key: the lookup is `reg.get(name)` on the "
        "name it was asked about, and what leaves the module is one of four "
        "fixed words (verified / UNVERIFIED / UNREGISTERED / UNKNOWN) plus a "
        "verdict sentence. The seat name that appears in the rendered line is "
        "the CALLER's — `seat_health._status` walks the seats directory and "
        "passes each name in; this module never learns a name the caller did "
        "not already print. THE SECOND SITE IS THE SAME SHAPE: "
        "`roster_runtimes` reads the register to answer which FAMILY owns each "
        "seat's credential wall, because a display name may differ from the "
        "family billed for it and only the launch metadata says so. It builds "
        "{key: (runtime, verified)} and every consumer reaches it as "
        "`runtimes.get(name)` on a name it already holds; what leaves is a "
        "family token from a fixed catalogue, never a roster key."),
    "actors.py": (
        "INTERNAL-MATCHING-ONLY, and the read is the CHECKED one on purpose: "
        "_own_roster_key asks roster_checked whether a name that is a RETIRED "
        "alias in the actor store is an exact roster key again (task/2338). "
        "What leaves is a bool plus an UNKNOWN sentence that names the "
        "caller-supplied name and the store's actor id — never a roster key; "
        "the fail-open reader would turn a read failure into permission to "
        "keep another generation's actor, which is the finding this closes. "
        "THE SECOND SITE IS `self_identity`, THE IDENTITY DERIVATION, AND IT "
        "IS AN EMISSION SITE. A process that declares no HELM_CHAT_NAME asks "
        "the roster which seat its session id binds; the read is the CHECKED "
        "one for the same reason as the first, because `seats_for_session`'s "
        "fail-open `roster()` answers `{}` for an unparseable file and that "
        "false zero told an operator to `helm chat join` INTO the state helm "
        "had just failed to parse. TWO things leave. (1) On exactly one hit "
        "the ROSTER ROW'S OWN SPELLING is returned as the seat NAME — "
        "ADDRESSING, not a render: it becomes the actuator key (casefold-exact "
        "on purpose, so argv casing cannot split one seat's beacon election) "
        "and every consuming door re-checks it through `admit_rostered`. (2) "
        "The AMBIGUITY refusal names every candidate row, and that is the one "
        "place roster KEYS are RENDERED here: each goes through "
        "`seats_common._seat_label` before it reaches the string, so a key "
        "carrying ESC or bidi loses it while a legitimate seat stays "
        "BYTE-IDENTICAL. The zero-match and UNKNOWN refusals interpolate only "
        "the caller's own session id and the roster PATH. NOT WIRED INTO THE "
        "SECTION-B SWEEP: that sweep drives named verbs against a table; the "
        "arm standing in for it lives beside the module, in "
        "tests/test_identity_layer.py "
        "(test_the_ambiguity_refusal_LAUNDERS_every_candidate_key)."),
    "seat_lifecycle.py": (
        "INTERNAL-MATCHING-ONLY: `measured_seat_route` reads the roster to "
        "index ONE row by a seat name the CALLER already holds (`rows.get("
        "canonical_seat(name))`), and returns that row's measured proxy ROUTE "
        "-- provider and upstream model ids, which are vendor strings and not "
        "roster keys. No roster key is bound, returned, printed or "
        "interpolated: the only name in the phrase `seat where` emits is the "
        "caller's own argument, which did not come from the roster."),
    "seat_identity.py": (
        "INTERNAL-MATCHING-ONLY: reads roster KEYS into the canonicals set "
        "for exact-match/classification; an emitted refusal string carries "
        "the OPERATOR-typed token and a canonical name that is itself "
        "operator-facing addressing (the suggestion IS the point), never a "
        "stored display field."),
    "work/_gc.py": (
        "INTERNAL-MATCHING-ONLY, and the FIRST SUBPACKAGE consumer — the "
        "latent hole _package_sources' recursion was written to close is now "
        "occupied. `_branch_triage` iterates roster KEYS solely as the "
        "right-hand operand of seats.recipient_matches, an equality test "
        "whose result is one BOOLEAN; no key is bound to a name, returned, or "
        "interpolated. The identity the triage line emits is the GIT "
        "COMMITTER string read out of `git show -s --format=%cn <%ce>` — the "
        "caller's own repository data on both branches of the wording — so a "
        "roster key cannot reach that sink even when the match succeeds."),
    "chatnode.py": (
        "LAUNDERED at the seam, AND IT IS AN EMISSION SITE. One site, in "
        "`_low_wake`: when a low-faucet spell opens, the roster resolves the "
        "integrator and proves the HELM_CHAT_NODE_OWNER seat is carried, for "
        "ONE unsigned chat post. A roster KEY reaches that post only as an "
        "@mention, and only after `_mentionable` proves it is the _seat_label "
        "launder's fixed point AND matches the mention alphabet; a key that "
        "fails either is not named at all, and the sentence saying so renders "
        "it through _seat_label. Driven by the runtime sweep's planted "
        "hostile seats."),
    "scratch.py": (
        "LAUNDERED at the seam, AND IT IS AN EMISSION SITE. One site, in "
        "`_wake`: when a seat's memory-pressure spell opens, the roster names "
        "the seat's lead and resolves the integrator for ONE chat post. A "
        "roster KEY reaches that post only as an @mention, and only after "
        "`_inert_seat` proves it is the _seat_label launder's fixed point AND "
        "matches the mention alphabet; a key that fails either is not named "
        "at all, never repaired into a different name. The seat named at the "
        "head of the post comes from the census's HELM_CHAT_NAME through the "
        "same door, and the room is a validated home_room or the literal "
        "`main`. Driven by the runtime sweep's planted hostile seats."),
    "stalebot.py": (
        "INTERNAL ADDRESSING: one fail-closed roster_checked() snapshot per "
        "sweep resolves BOTH the current proposal actor and the current review "
        "recipient. Dispatch source-session ownership outranks historical "
        "sender spelling; every casefold/session ambiguity refuses to the "
        "integrator instead of selecting a roster key by order. The selected "
        "key is an exact validated seat token used as the DM/dispatch address; "
        "runtime_for_session reads only the dispatch source session's verified "
        "runtime before family_for. No arbitrary roster key is interpolated: "
        "selected keys travel only through token-bound address fields, while "
        "unavailable/ambiguous facts render fixed prose."),
}

# COUNT-PIN per module: module-granularity alone let a NEW roster() call site
# slip into an ALREADY-allowlisted module unreviewed (r6 was module-only). Pin
# the exact number of call sites per module so a new one — even in an
# allowlisted module — trips the wire until a human re-counts AND confirms the
# new site launders. Regenerate deliberately with _roster_call_sites() below.
_ROSTER_CALL_COUNTS = {
    "ownership_census.py": 1,
    "turnstamp.py": 1,  # ONE read, in `reconcile`, and the count is the
                        # point: a SECOND site here would mean something
                        # other than the binding check is consulting the
                        # roster from a writer that runs inside the Stop
                        # dispatch, where a slow or failing read costs the
                        # operator their stop.
    "fixedtext.py": 1,  # ONE read, in `live_rows`; the survey and both
                        # renderers take the rows as an argument.
    "seats_integrator.py": 1,  # ONE read, in integrator_seat, and the whole
                               # module exists to make it the only one: the
                               # role is resolved at the moment of use rather
                               # than spelled into a send. A second site here
                               # would mean a caller answering the who-is-the-
                               # integrator question somewhere else, which is
                               # the defect this module was built to close.
    "landreq.py": 2,
    "seat_reassign.py": 2,  # TWO EXACT-KEY READS, one per resolution
                            # question. resolve_source asks whether a token
                            # names a seat (after trying it as a SESSION
                            # first, which is the point of the verb);
                            # resolve_target asks the same of the target;
                            # and resolve_target asks a second time for the
                            # FAMILY fallback, where more than one member is
                            # an ambiguity to report rather than a tie to
                            # break. None of the three mints a miss and none
                            # renders a name the operator did not supply.
    # 4 -> 5: TWO exact iterators + THREE membership readers. `census` joined
    # them to key each seat's consumption census on its own session and home
    # room; it binds no roster KEY and reaches only the seat it was handed.
    # 5 -> 6: `_record_repairs` writes each pane repair's OUTCOME back onto
    # the attendance row it belongs to — a second write after the act, the
    # same shape and the same reason as `_ack_alerts`. It reaches rows only
    # through get() and emits no key to any sink.
    # 6 -> 7: `settle_repair` is the CHILD's door onto that same write. The
    # repair's outcome is known in the process the repair spawned, not in the
    # sweep that started it, so the child settles its own episode — same
    # get()-only row access, same second-write-after-the-act shape, and the
    # seat it names arrives as an argument it never emits.
    # 7 -> 8: `install_repair` OPENS the episode before the spawn, which is
    # the same row, the same get()-only access and the same write-back door as
    # the three below it — it is a write BEFORE the act rather than after,
    # which is the whole point of it, and changes nothing about what it reads.
    "beacons.py": 8,
                           # roll() binds roster keys to build the census roll;
                           # attend() binds validated keys to reconcile a
                           # casefold-identical census seat with its ENROLLED
                           # display spelling before updating that existing row.
                           # Neither mints a miss, and their outward renderers
                           # launder through label(). escalate() and _ack_alerts()
                           # remain exact `r.get(seat)` membership probes over
                           # handed transition rows: they bind no roster key and
                           # skip a miss. The shape test below pins function,
                           # ITERATES/MEMBERSHIP polarity, AND mapping attrs, so
                           # this count cannot bless an arbitrary new reader.
    "cell.py": 2,          # seat_reading's fleet-actor check: ONE `.get(name)`
                           # membership lookup on a name it already holds; the
                           # miss returns "" and no key is ever bound.
                           # +1 (task/3049): _session_dispute, asked only where
                           # the owner's profile would stand — which rows bind
                           # this process's session; the one key it can return
                           # goes through seats_common._seat_label first
    "chat.py": 3,          # THREE SITES, AND TWO OF THEM CAN EMIT.
                           # (3) _tree_roster, the tree rung's read — its
                           # consumer tree_steers EMITS a key and that row's
                           # home room inside a pasteable command, each
                           # validated at the seam exactly as (1) validates
                           # its key, or not named at all; hostile-key and
                           # hostile-room arm in tests/test_chat_tree_steer.py
                           # (1) _spawn_roster, the spawn steer's read — its
                           # consumer _live_same_family EMITS a key inside a
                           # pasteable command, validated at the seam
                           # (_seat_label-unchanged AND dispatches._TOKEN) or
                           # not named at all; hostile-key arm in
                           # tests/test_chat_spawn_steer.py
                           # (2) _touch_poster_presence's roster_acquired —
                           # INTERNAL-MATCHING-ONLY, and the count pin is the
                           # only guard that could have seen it arrive, since
                           # chat.py was already allowlisted. It answers ONE
                           # question: is this poster name a seat at all, so
                           # that a SUBSYSTEM LABEL (who="dispatches" and its
                           # siblings) is skipped instead of tripping the
                           # cross-seat identity warning. The rows are read as
                           # a lowered KEY SET for a membership test and no key
                           # is bound, returned, rendered or logged — the name
                           # that reaches any sink is the caller's own
                           # argument, which did not come from the roster.
    "codexhomes.py": 1,
    "dispatches.py": 2,   # BOTH via roster_checked(), invisible to the old
                          # regex: _open_for's visibility filter and the
                          # recipient-runtime read. INTERNAL-MATCHING-ONLY.
    "fleet.py": 2,        # 1 -> 2. The first is via roster_checked() and is
                          # LAUNDERED at _seat_for; the raw key reaching the
                          # census's first column is the failure this pin
                          # exists to catch. The second is
                          # _join_resolved_model, which indexes rows by the
                          # seat name the census row ALREADY carries (that
                          # laundered one) and emits only the row's measured
                          # provider and upstream model — vendor strings, not
                          # roster keys — so it adds a reader and no sink.
    "hooks.py": 3,        # +1 (was 1): the second is roster_checked(), which
                          # the regex `\broster\(\)` could not match at all.
                          # +1 (was 2): unsigned_panes' one roster_checked()
                          # per scan asks the signing gate's single question
                          # about a pane's OWN declared name — is it a fleet
                          # actor (home_room) — through `.get(name)`; it binds
                          # no roster key, and both names in its reason are
                          # the pane's environ values, laundered through
                          # seats._seat_label.
    "injection_config.py": 1,  # one fail-closed roster acquisition; raw keys
                               # drive matching only, and the selected key is
                               # laundered through _seat_label before JSON emit
    "orcaadopt.py": 3,    # +2 (was 1). roster_current_sids(), the ADDRESSING
                          # half of the roster read once per pane listing: it
                          # EMITS (see its reason above) and is laundered at
                          # the sink in seat._panes. And _alias_rows(), which
                          # resolves a rename alias for _declared_verdict when
                          # the caller handed no rows — a roster_checked read,
                          # INTERNAL-MATCHING-ONLY (the key it finds is
                          # compared to `seat`, never printed), and every name
                          # that reaches that rung's refusal prose goes through
                          # seats_common._seat_label first. The original site
                          # stays INTERNAL-MATCHING-ONLY too.
    "proxywatch.py": 4,  # health() plus the session->identity and identity->
                          # session proof joins, all INTERNAL-MATCHING-ONLY,
                          # plus _session_keyed_seats: its returned names are
                          # consumed by membership at both call sites, and its
                          # refusals ARE emitted, laundered at the sink.
    # 2 -> 5: the DEAF-IN-EFFECT pane repair added three reads, none of which
    # emits a roster key. `_seat_session` returns a SESSION for the manual
    # repair door, and `_seat_waited` and `_still_owed` read only `home_room`
    # so the consumption census is taken from the seat's own room.
    # 5 -> 6 (task/2463 r6 finding 3): `_deaf_attendance` reads ONE seat's
    # `attendance` so the manual --nudge binds its episode to the alarm the
    # automatic repair would; it emits no roster key and returns the row or
    # None.
    # 6 -> 7: `_roster_row` is the ONE strict read behind a prepared
    # DEAF-IN-EFFECT keystroke, its non-blocking validation, and the attempt
    # standing a charge or a launch is decided on. It resolves the canonical
    # key of a seat the caller already names, returns that seat's row, and
    # emits no roster key.
    "reviewer_eligibility.py": 1,
                          # ONE read, `_read_roster`, and the count is the
                          # contract: `helm reviewers` answers about ONE
                          # INSTANT, so a second read would let the candidate
                          # set change between two lines of one table. What
                          # the site does with the value: the keys become the
                          # candidate set (raw, for matching against the tier
                          # policy, the usability join and the pane read), and
                          # each one becomes a report value exactly once,
                          # through `_label`. It hands the mapping to nothing
                          # that writes — this verb is read-only by
                          # construction, down to `repair=False` on the pane
                          # tail — so it owes no _PATH_CALL_WRITEBACKS entry.
    "resumeturn.py": 7,   # _native_registered membership + _recovery_owner's
                           # canonical casefold->recipient identity index. The
                           # latter refuses every assignee when the checked
                           # roster is unreadable.
    # REMOVED, NOT ZEROED (task/2333): the lineage walk moved to
    # seats_lineage.py and took this file's only roster read with it. A pin of
    # 0 does not work here and the arm says why — the scanner answers a dict
    # of files that HAVE sites, so a zeroed entry is an EXTRA KEY the actual
    # never carries and the equality fails on the pin rather than on the code.
    # seat_launch_assets.py: 1 -> absent.
    "seat_health.py": 1,   # UNCHANGED from trunk, and that is the finding
                           # rather than the absence of one: slice one's
                           # population note reads the roster PATH strictly
                           # instead of calling roster(), so this census does
                           # not see it. See the allowlist reason above.
    "seat_reachability.py": 1,  # capture freezes one checked roster snapshot;
                                   # exact-token matching only, no roster-key emit
    "seat_resume_all.py": 1,  # ONE roster_checked() in cmd_resume_all: the
                              # adopted-seat enumeration, laundered at the
                              # table's single emit site (Row.line)
    "seat_rehome.py": 1,      # ONE roster_checked() in verify: the register
                              # agreement check. The plan's OWN session read
                              # goes through orcaadopt.roster_identity and is
                              # counted under orcaadopt.py, so a SECOND site
                              # here would mean this module started reading
                              # the roster for an identity the plan already
                              # resolved. INTERNAL-MATCHING-ONLY.
    "seat_usability.py": 2,  # _read_roster's single register read per join
                             # pass, called at its use site (a bound accessor
                             # is invisible to this pin and _refuse_escapes
                             # rejects it). Its TEST SEAM is `register`, not
                             # `roster`: named for the accessor, the injected
                             # callable's own call site counted here too and
                             # this pin read 2 — correctly, since a local
                             # shadowing the accessor name is exactly the
                             # ambiguity the guard exists to notice.
                             # INTERNAL-MATCHING-ONLY.
                             # THE SECOND: roster_runtimes, the family-billing
                             # lookup. Keyed reads only (`runtimes.get(name)`
                             # on a name the caller supplied) and it emits a
                             # catalogued family token, never a roster key.
    "actors.py": 2,         # TWO CHECKED READS, one per identity question.
                            # THE FIRST: _own_roster_key, roster_checked ->
                            # exact-key membership for a retired-alias record;
                            # UNKNOWN refuses (task/2338).
                            # INTERNAL-MATCHING-ONLY.
                            # THE SECOND: self_identity, the derivation a
                            # process with no declared name depends on —
                            # roster_checked, then roster_indexes /
                            # seats_for_session_in over the SAME acquired
                            # snapshot. The count is the point: the acquisition
                            # is ALL-OR-NOTHING and happens BEFORE any count,
                            # so a THIRD site here would mean a second,
                            # separately-acquired read answering the same
                            # question — which is how UNKNOWN degrades back
                            # into a false zero. It EMITS: the one-hit return
                            # is the roster row's spelling used as ADDRESSING,
                            # and the ambiguity refusal renders every candidate
                            # key through seats_common._seat_label.
    "seat_identity.py": 1,  # _canonical_sources: roster KEYS -> the
                            # canonicals membership set; INTERNAL-MATCHING-
                            # ONLY (refusals emit operator-facing names)
    "seat_lifecycle.py": 1,  # measured_seat_route: ONE indexed read of the
                             # row the caller already names, for its measured
                             # ROUTE; INTERNAL-MATCHING-ONLY (see its reason)
    # SPLIT, NOT DRIFT — and the arithmetic is the proof. The seats.py split
    # moved four identity/addressing call sites into seats_identity.py:
    # seats.py 28 -> 24, seats_identity.py 0 -> 4, TOTAL 52 -> 52. Not one
    # site was added, removed, or edited, so no site needs re-reviewing for
    # laundering; the conservation of the total is what says so. A pin
    # updated without that check is a rubber stamp.
    "seats_identity.py": 2,
    "seats_common.py": 3,   # THE ONE DOOR (task/2139): canonical_keys and
                            # seat_row each read the roster once, to answer
                            # which STORED key is the identity a caller
                            # spelled. This pair is not new consumption --
                            # seats_identity 4 -> 2, seat_reassign 3 -> 2 and
                            # seat.py 1 -> 0 are the sites that stopped
                            # indexing the roster by a caller-supplied name,
                            # so TOTAL falls by 4 and rises by 2. Nothing
                            # gains a site; four lose one each.
                            # task/2338 ADDS ONE, and it is the same
                            # question through the same door: live_alias
                            # reads the roster to answer which STORED key a
                            # spelled name is a live rename alias OF. What
                            # leaves is a stored key or None -- RESOLUTION-
                            # ONLY, like its two siblings; every consumer
                            # (own_name, deliverable, the recipient door,
                            # the beacon assertion) asks here instead of
                            # indexing the roster by the caller's spelling.
    # SPLIT, NOT DRIFT (second wave): seven sites moved to seats_roster.py.
    # seats.py 24 -> 17, seats_roster 0 -> 7, TOTAL 52 -> 52 — conserved,
    # so nothing needs re-reviewing for laundering.
    #
    # NOT CONSERVED, DELIBERATELY (task/910, 2026-08-10): seats_roster 7 -> 2,
    # TOTAL 54 -> 49, and NOTHING gains a site. Five of those seven were the
    # WHOLE-FILE WRITERS — write_roster, disown_session, rename_seat,
    # rehome_seat, set_mute — each doing read-all then write-all off the
    # FAIL-OPEN reader, which probe-provably DESTROYS every row it cannot
    # parse. All five were RE-POINTED (none edited in place) at
    # seats_common.roster_for_write.
    #
    # AND THE DROP IS FIVE, NOT FOUR, because that guard does its own single
    # open+json.load rather than calling roster(). An intermediate cut called
    # roster() and then re-read the file to classify it — two reads of one
    # fact, the exact defect class this lane cures — and collapsing that to
    # one read removed the last consumer this counter would have gained.
    #
    # WHY A DROP IS NOT A RUBBER STAMP HERE. This counter exists because a NEW
    # call site can reach a sink unreviewed. Five sites collapsing into a
    # reader that is not one adds no unreviewed path — it removes five — and
    # the two left in seats_roster are pure readers that were never part of
    # the write hazard. An AST arm in tests/test_roster_write_guard.py now
    # refuses a SIXTH whole-file writer that binds the fail-open reader at
    # all, so the shape is pinned independently of this count.
    # 2 -> 3 (2026-08-10): roster_checked now DELEGATES to roster_acquired
    # instead of doing its own open+json.load, so the reader that was one site
    # is two — the shared core plus the narrowing door. No new PATH into the
    # roster: the second site is roster_checked calling the first, and its
    # all-or-nothing contract is unchanged, which is what the mint and the
    # write guard depend on.
    # EXTRACTED, AND THE ARITHMETIC IS THE PROOF: seats_roster 3 -> 2,
    # seats_mute 0 -> 1, TOTAL unchanged. `mutes()` moved with the wake
    # filter; not one roster read was added, removed or edited.
    "seats_mute.py": 1,
    "seats_roster.py": 2,
    # MOVED, AND THE ARITHMETIC IS THE PROOF (task/2333): seat_launch_assets
    # 1 -> 0, seats_lineage 0 -> 1, TOTAL unchanged — not one roster read was
    # added, removed or edited. What DID change is who reaches it: landreq
    # consumes this walk now, which is a new CONSUMER of an old site and is
    # reviewed in the rationale above rather than waved through by the
    # conservation. seats_roster stays at 3: the reader lives
    # BESIDE that module and never inside it, because the seats split budget
    # is what drains the facade and a new concern may not be parked in a file
    # already standing at its line.
    "seats_lineage.py": 1,
    # third wave: three sites to seats_delivery. 17 -> 14, 0 -> 3, 52 -> 52.
    "seats_cursor.py": 2,  # restore sweep + identity-generation matching
                            # paths; only pair counts are rendered
    "seats_delivery.py": 3,
    # task/2069: one tri-state claims snapshot, INTERNAL-MATCHING-ONLY.
    # RELOCATED, not new: the claims rung left seats_stop_guard for
    # seats_stop_claims and its single roster read went with it. The census
    # TOTAL is unchanged and seats_stop_guard drops by the same 1 — a moved
    # read, never an unreviewed sink.
    "seats_stop_claims.py": 1,
    # fourth wave: one site to seats_stop_signals. 14 -> 13, 0 -> 1, 52 -> 52.
    "seats_stop_signals.py": 1,
    # fifth wave: four sites to seats_ack. 13 -> 9, 0 -> 4, 52 -> 52.
    "seats_ack.py": 4,
    "seats_claims.py": 1,  # claim_holder_listedness — ONE membership read
                           # over roster KEYS via recipient_matches, answering
                           # listed/unlisted/unknown. It binds no key: the
                           # three words and the marker sentence are constants,
                           # and the holder name printed beside them comes from
                           # the CLAIM row, laundered by the caller.
    # sixth wave: seven sites moved to seats_report — the rendering half.
    # Two whole-roster writers use the fail-closed roster_for_write door, and
    # set_status adds one roster_checked preflight so a missing target can be
    # refused without laundering an unreadable roster into clean-empty state.
    # The preflight is INTERNAL-MATCHING-ONLY: it looks up the operator-supplied
    # seat and emits no roster key. Six display/matching reads remained.
    # SIX IS NOW FIVE: gc_roster's read left with it to seats_gc when
    # seats_report hit 999 of its 1000-line budget. The count moved, the site
    # did not change, and neither did which door each writer uses — the GC
    # still reads roster() to scan and roster_for_write() to apply.
    "seats_report.py": 5,
    "seats_gc.py": 1,
    "chatdebris.py": 1,    # ONE snapshot, in unpaired_session_cursors;
                           # only paths and counts leave the module.
    "seats_receipts.py": 3,  # append census + current rename/sink resolver +
                              # sessionless historical-key attribution; display
                              # keys are laundered through _seat_label.
    # seventh wave: one site to seats_work_offer. 2 -> 1, 0 -> 1, 52 -> 52.
    "seats_work_offer.py": 1,
    # seats.py IS GONE FROM THIS PIN, and its absence is the finish
    # line: the facade reads the roster ZERO times now. An allowlist
    # entry for a module that no longer consumes would rot into a
    # vouch for a call site that does not exist.
    "seats_cli.py": 1,     # ONE read: the ROSTER verb's single snapshot.
                           # Both legs of `chat status` resolve through
                           # seat_row instead, so neither indexes the roster
                           # by a caller's raw token and one verb cannot
                           # answer "no roster row" to a spelling its other
                           # leg accepts. That is a MOVE of the consumer, not
                           # a removal: seats_common carries both of its
                           # sites in this same pin.
                           # The ROSTER verb takes one snapshot
                           # of its own. It used to read a second time for the
                           # claim marks while roster_report read for the seat
                           # list — two acquisitions, one screen, and the window
                           # between them is how one render came to list a seat
                           # above and call the same holder unlisted below. The
                           # report reads once and stamps each claim's
                           # listedness from that read. What remains is the verb
                           # dispatcher's own target lookup PLUS the ONE
                           # per-render snapshot the CLAIMS verb takes. Both snapshots feed
                           # only claim_marks / claim_holder_listedness, which
                           # return fixed state words and constant sentences —
                           # no roster KEY is bound or emitted from either, and
                           # the holder shown beside them comes from the CLAIM
                           # row through _seat_label. Counted deliberately: the
                           # alternative was reading the roster once PER CLAIM,
                           # which let two rows on one screen describe two
                           # different rosters.
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
                           # main's 19 (+3 ack-ladder, +1 _seat_checked's
                           # membership probe) + work-offer's
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
    "chatnode.py": 1,     # `_low_wake`: one read per opened low-faucet
                          # spell, keys leave only as _mentionable @mentions
    "scratch.py": 1,      # `_wake`: one read per opened spell, keys leave
                          # only as _inert_seat-validated @mentions
    "stopfacts_resident.py": 1,  # `compute`: one read per refresh, keys used
                                 # only to select and index seats
    "stalebot.py": 3,     # THREE distinct operations, one fail-closed read each:
                          # sweep classification (_seat_roster), explicit cured
                          # redispatch, and digest delivery (_post). The sweep read
                          # is shared across every cured row; actuator and delivery
                          # re-read because they execute later and must prove the
                          # current reviewer/wake target at their own boundary.
                          # Selected keys escape only as exact validated addresses;
                          # ambiguity routes to the integrator or refuses.
    "todos.py": 1,
    "web:_api_chat": 1,
    "web:_dm_seat_map": 1,
    "web:_rooms_summary": 1,
    # web:_roster_cached IS DELIBERATELY ABSENT AGAIN. An intermediate cut gave
    # the web roster its own roster_checked() to stamp claim listedness — the
    # right OUTPUT reached by a second read of a fact the report had already
    # read, which is the exact defect this lane cures, reproduced inside the
    # cure. roster_report stamps it now, so the web consumes NO roster of its
    # own and an entry here would rot into a vouch for a call site that does
    # not exist, exactly as the seats.py note below says.
    "work/_gc.py": 1,     # NEW 2026-08-05, and the first non-top-level entry:
                          # _branch_triage's rostered-committer predicate. ONE
                          # iterating read whose every key goes straight into
                          # seats.recipient_matches as an equality operand;
                          # the value that escapes is a bool. The triage line
                          # prints the GIT committer string, never the key it
                          # matched. Until this landed, "no subpackage
                          # consumes the roster today" was true of the tree —
                          # the recursion in _package_sources is what caught
                          # this one on its first gate rather than never.
    "work/_guard.py": 1,  # NEW 2026-08-09: the seat-name rung's install-time
                          # staleness check. ONE read, and every key is an
                          # equality operand against the authority file's
                          # contents; what escapes is a COUNT and a path. The
                          # names are deliberately not printed — an install
                          # summary listing roster keys would be an emission
                          # site, and this rung exists to keep seat identities
                          # out of places they do not belong.
}

# EVERY accessor that hands a caller the roster mapping. `roster()` is the
# fail-OPEN reader; `roster_checked()` returns the SAME mapping plus a probe
# flag. Counting only `roster()` made four live consumers — dispatches.py,
# fleet.py, orcaadopt.py, resumeturn.py — structurally INVISIBLE to a guard
# whose whole claim is that it is total across the tree. That blindness is not
# bookkeeping: fleet.py obtains the roster through `roster_checked()` and, at
# the time of writing, prints the resulting KEY raw to the operator's terminal
# and raw into `fleet --json` (#137 P0). The tripwire built to make that class
# un-reopenable could not see the module where it reopened.
# EVERY DOOR THAT OBTAINS THE ROSTER, and the third one was added the same
# hour it was written. `roster_acquired` returns the FAIL-OPEN rows beside the
# verdict on them, so it hands a caller exactly the keys `roster()` does and is
# a roster consumer by every argument this guard rests on. Leaving it out would
# have made a brand-new reader the one path into the tree that no allowlist
# covers — a hole opened by the cure for a different hole, and invisible
# precisely because the count in an already-allowlisted module went DOWN.
_ROSTER_GETTERS = ("roster", "roster_checked", "roster_acquired")


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


# THE REAL TREE IS SCANNED ONCE PER PROCESS (task/3039). The site scanners
# below were called again by every arm that reads them -- 3,491 `ast.parse`
# calls, 95% of this module -- and helm/ does not change while one process
# runs. Only a call about the real package is remembered: a planted package is
# a tree an arm built to hold a probe, so it is scanned every time, and so is
# any call made while PKG itself is pointed elsewhere. Answers are copies.
_REAL_PKG = PKG
_REAL_SCANS = {}


def _real_tree_once(scan):
    @functools.wraps(scan)
    def remembered(*args, **kw):
        if PKG != _REAL_PKG or any(v != _REAL_PKG
                                   for v in args + tuple(kw.values())):
            return scan(*args, **kw)
        if scan.__name__ not in _REAL_SCANS:
            _REAL_SCANS[scan.__name__] = scan(*args, **kw)
        got = _REAL_SCANS[scan.__name__]
        return set(got) if isinstance(got, set) else list(got)
    return remembered


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


@_real_tree_once
def _roster_call_sites(pkg=PKG):
    """[(module, lineno, text)] for every site under pkg that OBTAINS the roster.

    AST, NOT A LINE REGEX. The predecessor grepped `\\broster\\(\\)` over raw
    lines, so a DOCSTRING or COMMENT that merely MENTIONED roster() counted as
    a call site. That is not theoretical: it cost lane/dispatch-send-unroutable
    -recipient a full r4 gate cycle — seats.py read 27 sites against a pin of
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
            "verify each site launders its emitted key, then update THE WHOLE "
            "SET, because a new reader owes every table this file keeps and "
            "they are checked by DIFFERENT arms: _ROSTER_CALL_COUNTS (here); "
            "_PATH_CALL_WRITEBACKS, if the site hands the mapping to "
            "write_json; and for beacons.py also _BEACONS_READER_CONTRACT "
            "plus the shape/membership set in "
            "BeaconsRosterReaderShapeTest. Updating one and re-running spends "
            "a whole gate to be told about the next one — measured twice on "
            "one lane in one session.\n  actual: %s\n  pinned: %s"
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
# nothing else here can catch: beacons.py's rationale has twice gone stale as
# readers changed altitude. The first pin caught roll() iterating behind prose
# that called every reader a membership probe. The casefold-display repair then
# legitimately made attend() iterate too: it must bind the enrolled roster key,
# not preserve a legacy beacon's casing or silently skip the row. A count or a
# shape-only map is insufficient now — it could bless a third arbitrary iterator
# or a reader that reaches the mapping through a wider accessor. Pin the bounded
# function -> (shape, attrs) contract and fail closed when the mapping escapes the
# syntax the scanner classifies. Scoped to beacons.py deliberately: it is the
# rationale measured here, and widening to unread modules would invent facts.

_ITERATING_ATTRS = ("items", "keys", "values")

# Positive contract: roll and attend are the ONLY iterators, for the two named
# owner-layer reasons above. Negative contract: escalate and _ack_alerts remain
# membership-only, and all three register readers reach rows only through get.
_BEACONS_READER_CONTRACT = {
    "roll": ("ITERATES", {"items", "session_owners"}),
    # MEMBERSHIP, not ITERATES: `census` walks the seat list `roll` already
    # produced and reaches each row through get() to read its session and
    # home room for the consumption census. It binds no roster KEY of its own.
    "census": ("MEMBERSHIP", {"get"}),
    "attend": ("ITERATES", {"get"}),
    # ITERATES now, not MEMBERSHIP: `escalate`'s pane-repair leg resolves the
    # roster's own spelling for each DEAF-IN-EFFECT census row, which binds
    # roster KEYS exactly as `attend` does. It was MEMBERSHIP while the leg
    # rode the notification transitions, which already carried resolved names.
    "escalate": ("ITERATES", {"get"}),
    "_ack_alerts": ("MEMBERSHIP", {"get"}),
    # MEMBERSHIP: the repair outcome write-back reaches each row through
    # get(), the same shape and the same second-write-after-the-act reason as
    # `_ack_alerts` beside it.
    "_record_repairs": ("MEMBERSHIP", {"get"}),
    # MEMBERSHIP: the same outcome write-back reached from the CHILD side.
    # `settle_repair` takes the seat as an argument, reads its row through
    # get(), and writes the mapping back without ever emitting a key.
    "settle_repair": ("MEMBERSHIP", {"get"}),
    # MEMBERSHIP: the episode is OPENED here, before the spawn, so the child
    # and the parent have one name for one spell. Same row access as the three
    # above — `get()` and a write-back — and no roster key is emitted.
    "install_repair": ("MEMBERSHIP", {"get"}),
}


#: The functions whose roster write-back goes through `seats_mod.roster_path()`
#: — `_ack_alerts` advances a delivery latch, `_record_repairs` records a pane
#: repair's outcome, and `settle_repair` is the same record written by the
#: CHILD that performed the repair. All three are second writes AFTER their
#: act, all three reach rows only through get(), and none of them reads the
#: mapping it hands to write_json.
_PATH_CALL_WRITEBACKS = ("_ack_alerts", "_record_repairs", "settle_repair",
                         "install_repair")


def _is_roster_writeback(function, call, value):
    """The classified calls that receive, but do not read, the mapping."""
    f = call.func
    path = call.args[0] if call.args else None
    if not (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
            and f.value.id == "pk" and f.attr == "write_json"
            and len(call.args) == 2 and call.args[1] is value
            and not call.keywords):
        return False
    if function == "attend":
        return isinstance(path, ast.Name) and path.id == "path"
    return (function in _PATH_CALL_WRITEBACKS
            and isinstance(path, ast.Call) and not path.args and not path.keywords
            and isinstance(path.func, ast.Attribute)
            and isinstance(path.func.value, ast.Name)
            and path.func.value.id == "seats_mod"
            and path.func.attr == "roster_path")


def _is_roll_identity_index(function, call, value):
    """The one exact read-only helper that receives roll's roster mapping."""
    f = call.func
    return (function == "roll" and isinstance(f, ast.Attribute)
            and isinstance(f.value, ast.Name) and f.value.id == "seats_mod"
            and f.attr == "session_owners" and call.args == [value]
            and not call.keywords)


def _reader_shapes(path):
    """{function: (shape, {attrs used on the mapping})} for roster readers.

    This is deliberately a BOUNDED syntactic proof, not general Python
    dataflow. It follows plain local aliases to a fixed point. Every load of the
    mapping or one of those aliases must then be an invoked mapping method, a
    direct iterator, roll's exact `sorted(rows)` iteration, its exact read-only
    `session_owners(rows)` identity index, or one of the persistence writebacks
    above; subscript reads, every other indirect iteration/call escape, accessor
    escape, and every unclassified use raise instead of silently scoring the
    reader as membership-only."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    out = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        sub = list(ast.walk(node))
        parent = {id(child): owner for owner in sub
                  for child in ast.iter_child_nodes(owner)}
        calls = [n for n in sub if isinstance(n, ast.Call)
                 and _called_name(n.func) in _ROSTER_GETTERS]
        if not calls:
            continue
        born = {}
        for call in calls:
            bindings = [a for a in sub if isinstance(a, ast.Assign)
                        and a.value is call and len(a.targets) == 1
                        and isinstance(a.targets[0], ast.Name)]
            if not bindings:
                raise RosterAccessEscape(
                    "%s:%d calls a roster accessor without binding the "
                    "mapping to a local name — the shape pin cannot tell an "
                    "iterating reader from a membership probe."
                    % (os.path.basename(path), call.lineno))
            for binding in bindings:
                name = binding.targets[0].id
                born[name] = min(born.get(name, binding.lineno), binding.lineno)

        # Bounded alias propagation. Assignment is deliberately the only
        # propagation form: containers, unpacking, expressions, and calls are
        # escapes, then the exhaustive use classification below refuses them.
        # The source must already have been bound: attend() legitimately uses a
        # comprehension-local `r` before its later roster local of the same name.
        while True:
            aliases = {}
            for a in sub:
                if not (isinstance(a, ast.Assign) and len(a.targets) == 1
                        and isinstance(a.targets[0], ast.Name)
                        and isinstance(a.value, ast.Name)
                        and a.value.id in born
                        and a.lineno >= born[a.value.id]):
                    continue
                name = a.targets[0].id
                if name not in born:
                    aliases[name] = min(aliases.get(name, a.lineno), a.lineno)
            if not aliases:
                break
            born.update(aliases)

        attrs, iterates = set(), False
        for use in sub:
            if not (isinstance(use, ast.Name)
                    and isinstance(use.ctx, ast.Load) and use.id in born
                    and use.lineno >= born[use.id]):
                continue
            owner = parent.get(id(use))
            above = parent.get(id(owner))
            if (isinstance(owner, ast.Assign) and owner.value is use
                    and len(owner.targets) == 1
                    and isinstance(owner.targets[0], ast.Name)):
                continue                    # classified alias propagation
            if isinstance(owner, ast.Attribute) and owner.value is use:
                if not (isinstance(above, ast.Call) and above.func is owner):
                    raise RosterAccessEscape(
                        "%s:%d lets roster accessor .%s escape without calling "
                        "it outright" % (os.path.basename(path), use.lineno,
                                         owner.attr))
                attrs.add(owner.attr)
                continue
            if (isinstance(owner, (ast.For, ast.comprehension))
                    and owner.iter is use):
                iterates = True
                continue
            if isinstance(owner, ast.Subscript) and owner.value is use:
                if (node.name == "attend" and isinstance(owner.ctx, ast.Store)
                        and isinstance(above, ast.Assign)
                        and owner in above.targets):
                    continue                # classified r[seat] = row writeback
                raise RosterAccessEscape(
                    "%s:%d reads or escapes a roster mapping subscript; the "
                    "bounded shape pin permits only its declared read forms"
                    % (os.path.basename(path), use.lineno))
            if isinstance(owner, ast.Compare):
                membership = any(isinstance(op, (ast.In, ast.NotIn))
                                 and rhs is use
                                 for op, rhs in zip(owner.ops,
                                                   owner.comparators))
                if membership:
                    attrs.add("__contains__")
                    continue
            if isinstance(owner, ast.Call):
                if (isinstance(owner.func, ast.Name)
                        and owner.func.id in {"list", "iter", "sorted"}
                        and use in owner.args):
                    if node.name == "roll" and owner.func.id == "sorted":
                        iterates = True
                        continue
                    raise RosterAccessEscape(
                        "%s:%d indirectly iterates the roster through %s(); "
                        "use a declared direct iterator so the shape stays "
                        "visible" % (os.path.basename(path), use.lineno,
                                     owner.func.id))
                if _is_roll_identity_index(node.name, owner, use):
                    attrs.add("session_owners")
                    continue
                if _is_roster_writeback(node.name, owner, use):
                    continue
                raise RosterAccessEscape(
                    "%s:%d passes the roster mapping to a call; the bounded "
                    "shape pin cannot prove what that callee reads"
                    % (os.path.basename(path), use.lineno))
            raise RosterAccessEscape(
                "%s:%d uses the roster mapping through unclassified %s syntax"
                % (os.path.basename(path), use.lineno,
                   type(owner).__name__ if owner is not None else "root"))
        if attrs & set(_ITERATING_ATTRS):
            iterates = True
        out[node.name] = ("ITERATES" if iterates else "MEMBERSHIP", attrs)
    return out


class BeaconsRosterReaderShapeTest(unittest.TestCase):
    def test_the_beacons_rationale_names_the_right_readers(self):  # noqa: VACUOUS_ASSERTION — _BEACONS_READER_CONTRACT is a six-entry literal, so an empty or rotted scan FAILS this assertEqual rather than passing it; the count-pin cross-check below is the second unconditional control
        """MUST-HIT first: the five readers the allowlist paragraph and the
        count-pin both describe must actually be the five the module has, so
        a rename or a new site fails here instead of leaving the prose
        quietly describing a module that moved."""
        shapes = _reader_shapes(os.path.join(PKG, "beacons.py"))
        self.assertEqual(sorted(shapes), sorted(_BEACONS_READER_CONTRACT),
                         "beacons.py's roster readers are not the ones the "
                         "allowlist rationale names — re-read the module, "
                         "then update BOTH _ROSTER_CONSUMERS['beacons.py'] "
                         "and _BEACONS_READER_CONTRACT")
        self.assertEqual(len(shapes), _ROSTER_CALL_COUNTS["beacons.py"],
                         "the shape pin and the count pin disagree about how "
                         "many roster readers beacons.py has")

    def test_exactly_one_beacons_reader_ITERATES_the_roster(self):  # noqa: VACUOUS_ASSERTION — historical trunk identity retained while the strengthened exact contract proves the now-intentional second iterator and rejects every undeclared one
        """The historical regression identity stays stable while its measured
        premise evolves: roll was once the only iterator; attend is now the one
        deliberate second iterator because casefold reconciliation must bind the
        enrolled roster key. Escalate and _ack_alerts remain membership-only.
        Pin mapping access too, so updating the count or polarity cannot bless
        `.keys()`/`.values()` or a fifth reader behind narrower prose."""
        shapes = _reader_shapes(os.path.join(PKG, "beacons.py"))
        self.assertEqual(
            shapes, _BEACONS_READER_CONTRACT,
            "a beacons.py roster reader changed function, shape, or mapping "
            "access. Iterators bind roster KEYS and require an explicit owner-"
            "layer rationale; membership readers may reach only their handed "
            "seat through get().\n  actual: %s\n  pinned: %s"
            % (shapes, _BEACONS_READER_CONTRACT))
        self.assertEqual(
            {fn for fn, (shape, _attrs) in shapes.items()
             if shape == "ITERATES"}, {"roll", "attend", "escalate"})
        self.assertEqual(
            {fn for fn, (shape, _attrs) in shapes.items()
             if shape == "MEMBERSHIP"},
            {"census", "_ack_alerts", "_record_repairs", "settle_repair",
         "install_repair"})

    def _mutated_ack_shapes(self, injected):
        path = os.path.join(PKG, "beacons.py")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        # ANCHORED ON THE REFUSAL ABOVE IT, because `_record_repairs` takes
        # the same lock and opens with the same two lines — the bare seam is
        # no longer unique, and this assertion caught that rather than
        # letting the mutation land in the wrong function.
        needle = ("                return False\n"
                  "            r = seats_mod.roster()\n"
                  "            hit = False\n")
        self.assertEqual(source.count(needle), 1,
                         "_ack_alerts fixture seam moved; re-anchor the mutation")
        replacement = ("                return False\n"
                       "            r = seats_mod.roster()\n" + injected
                       + "            hit = False\n")
        with tempfile.NamedTemporaryFile("w", suffix=".py") as f:
            f.write(source.replace(needle, replacement))
            f.flush()
            return _reader_shapes(f.name)

    def test_shape_scanner_follows_alias_iteration(self):
        shapes = self._mutated_ack_shapes(
            "            widened = r\n"
            "            again = widened\n"
            "            for _probe in again:\n"
            "                break\n")
        self.assertEqual(shapes["_ack_alerts"], ("ITERATES", {"get"}))
        self.assertNotEqual(shapes, _BEACONS_READER_CONTRACT,
                            "an alias iterator must break the membership-only "
                            "_ack_alerts contract")
        contains = self._mutated_ack_shapes(
            "            _probe = 'unrelated' in r\n")
        self.assertEqual(contains["_ack_alerts"],
                         ("MEMBERSHIP", {"get", "__contains__"}))
        self.assertNotEqual(contains, _BEACONS_READER_CONTRACT,
                            "a direct membership form outside declared get() "
                            "must remain visible to the exact contract")
        self.assertEqual(_reader_shapes(os.path.join(PKG, "beacons.py")),
                         _BEACONS_READER_CONTRACT,
                         "the real four-reader contract is the positive control")

    def test_shape_scanner_rejects_mapping_escapes(self):
        cases = {
            "subscript": (
                "            _probe = r['unrelated'] if 'unrelated' in r "
                "else None\n", "mapping subscript"),
            "alias-subscript": (
                "            widened = r\n"
                "            _probe = widened['unrelated']\n",
                "mapping subscript"),
            "list": ("            list(r)\n", "indirectly iterates"),
            "iter": ("            iter(r)\n", "indirectly iterates"),
            "sorted": ("            sorted(r)\n", "indirectly iterates"),
            "alias-call": (
                "            widened = r\n"
                "            again = widened\n"
                "            opaque(again)\n", "passes the roster mapping"),
            "accessor-call": (
                "            opaque(r.get)\n", "accessor .get escape"),
        }
        for name, (injected, message) in cases.items():
            with self.subTest(name=name):
                with self.assertRaisesRegex(RosterAccessEscape, message):
                    self._mutated_ack_shapes(injected)


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
        # leaks (task/62). Written via chat.post because seats.dm itself
        # refuses a non-token addressee — the lane file is the substrate.
        from helm import chat as _chat
        _chat.post("dm to the hostile seat", _chat.dm_room(cls.SEAT),
                   who="planter")

    def _assert_inert(self, label, text):
        self.assertNotIn(ESC, text, "%s leaked ESC" % label)
        self.assertNotIn(BIDI, text, "%s leaked bidi" % label)

    def test_the_scratch_pressure_wake_names_no_hostile_key(self):
        """scratch._wake reads the roster for a lead and names the pressured
        seat: every hostile key planted here is homed in the room the wake
        searches, fresh, in a checkout — the exact shape a lead has — and
        none may reach the post."""
        from helm import scratch, seatceiling
        rows = {k: dict(v, home_room="helm", cwd="/x/helm",
                        last_seen=time.time())
                for k, v in seats.roster().items() if isinstance(v, dict)}
        self.assertIn(self.SEAT, rows, "control: the hostile key is on the "
                                       "roster this wake reads")
        p = seatceiling.Pressure("/cg/agents-x.slice", 104, 100, 1.04, None,
                                 (1,), (1,), seatceiling.THROTTLED, None)
        said = []
        got = scratch._wake(p.slice, p, {"slice_seats": {p.slice: self.SEAT}},
                            None, roster=rows,
                            post=lambda t, r: said.append(t + r) or {"id": "x"})
        self.assertIsNotNone(got)
        self.assertTrue(said, "control: the wake was posted")
        for text in said:
            self._assert_inert("scratch pressure wake", text)

    def test_the_low_faucet_wake_names_no_hostile_key(self):
        """chatnode._low_wake reads the roster for the integrator and the
        node owner: the hostile key is planted as BOTH — a roster key ending
        in the integrator suffix, and the seat HELM_CHAT_NODE_OWNER names —
        and neither may reach the post."""
        from helm import chatnode
        hostile_integrator = "night" + ESC + "[2J" + BIDI + "-integrator"
        rows = {k: v for k, v in seats.roster().items() if isinstance(v, dict)}
        self.assertIn(self.SEAT, rows, "control: the hostile key is on the "
                                       "roster this wake reads")
        rows[hostile_integrator] = {"home_room": "helm"}
        fa = {"state": "funded", "cell": "4a" * 32, "balance": 2000,
              "grant": 697, "low_grants": 5, "low_below": 3485,
              "grants_left": 2, "low": True}
        said = []
        prior = os.environ.get(chatnode.OWNER_ENV)
        os.environ[chatnode.OWNER_ENV] = self.SEAT
        try:
            got = chatnode._low_wake(
                "http://127.0.0.1:8898", fa, roster=rows,
                post=lambda t, r: said.append(t + r) or {"id": "x"})
        finally:
            if prior is None:
                os.environ.pop(chatnode.OWNER_ENV, None)
            else:
                os.environ[chatnode.OWNER_ENV] = prior
        self.assertEqual(("main", []), got, "neither hostile key is mentioned")
        self.assertTrue(said, "control: the wake was posted")
        for text in said:
            self._assert_inert("low-faucet wake", text)

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

    def test_config_injection_observation_is_inert(self):  # noqa: VACUOUS_ASSERTION — the selected hostile roster key must survive as a laundered nonempty target while exact inequality proves the raw key did not escape
        catalog = injection_config._catalog_rows
        injection_config._catalog_rows = lambda _session: ([], None)
        try:
            body = injection_config.view(session="z" * 32, rows=[])
        finally:
            injection_config._catalog_rows = catalog
        self._assert_json_inert("/api/config/injection", body)
        self.assertEqual(body["target"]["seat"]["value"],
                         seats._seat_label(self.SEAT))
        self.assertNotEqual(body["target"]["seat"]["value"], self.SEAT)

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
        2026-08-04. The sweep drove every other module's sinks and never this
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

    # -- seat_resume_all.py: the post-reboot sweep's table ---------------------
    def test_seat_resume_all_table_is_inert(self):
        """The sweep enumerates orca-ADOPTED seats from roster KEYS and prints
        each as its table's first column. MUTATION: render self.seat raw in
        Row.line — the planted key's ESC/bidi reach the operator's terminal."""
        from helm import harness, orcaadopt, seat_resume_all

        class Fake(harness._CLIAdapter):
            name, path = "orca", "/bin/orca"

            def list(self):
                return []

        fake = Fake()
        # autospec, not a hand-typed lambda: the liveness reader passes
        # `census=`, a double without it raises there, and that reader
        # swallows the error instead of reading the planted None.
        with unittest.mock.patch.object(orcaadopt, "resolve", autospec=True,
                                        return_value=None), \
                unittest.mock.patch.object(harness, "detect",
                                           return_value=fake):
            text, rc = self._cap(
                lambda: seat_resume_all.cmd_resume_all([]))
        self._assert_inert("helm seat resume --all", text)
        self.assertEqual(rc, 0, text)
        self.assertIn("lane", text)              # the laundered name survives
        self.assertIn("pwn", text)
        self.assertIn("UNKNOWN", text)           # and the row really rendered

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


@_real_tree_once
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
                                "text": "@daria look here", "turn": "t1"},
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
        self.assertIn("undelivered", text)         # the block fired (control)…
        # …and renders NO row content at all — task 692 dropped the sample
        # rows, so the idle gate no longer reads or emits a row's from-field.
        # The block is now inert BY CONSTRUCTION, not by laundering: neither
        # the planted body nor the from's de-fanged remnant can reach this sink.
        self.assertNotIn("ping here", text)        # the row body is not shown
        self.assertNotIn("pwn", text)              # nor the from's visible tail

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


@_real_tree_once
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
        "LAUNDERED: the ack verbs read a chat row's from/tfrom to decide "
        "which rows are ADDRESSED to the acting seat. Identity that reaches "
        "the operator goes through _seat_label; the rest are comparisons. "
        "Moved verbatim from seats.py by the split."),
    "seats_catchup.py": (
        "LAUNDERED / INTERNAL-MATCHING-ONLY: catchup reads from/rfrom/dm to "
        "classify direct asks and tally their senders. Rendered senders pass "
        "through chat._dsan; all other reads are recipient comparisons."),
    # seats_stop_guard.py removed (task 692): the idle gate's one from-field
    # read lived in the inbox block's sample rows; trimming the block dropped
    # the samples and the read with them, so the gate no longer consumes a
    # chat-row identity at all (see the count-pin note below).
    "seats_ack.py": (
        "LAUNDERED / INTERNAL-MATCHING-ONLY: this is the module whose whole "
        "subject is a chat row's identity fields — it reads from/tfrom to "
        "locate a row and to decide who owes an ack. The identity that "
        "reaches a human surface goes through _seat_label; the rest are "
        "comparisons. Moved verbatim from seats.py by the split."),
    "route.py": (
        "NOT-A-CHAT-ROW: `report[\"from\"]` on the routing verb's own report "
        "is the MODEL FAMILY the caller asked from — `--from fable` resolved "
        "to the credential behind it. It never came from a chat row, it is "
        "one of seven values in the seat catalog's family vocabulary, and "
        "the header compares it against the spelling the caller typed so a "
        "reader sees that an alias was resolved. Seat NAMES in that verb do "
        "reach the operator, and every one of them goes through the roster "
        "verb's own label door."),
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
    "machine_senders.py": (
        "INTERNAL-MATCHING-ONLY: owner_rail reads a chat row's from ONCE, to "
        "test it against the owner's names beside the row's owner-rail "
        "origin, and returns a bool that decides whether the beacon holds "
        "the row for the hook. No name is emitted; the row itself reaches a "
        "seat only through the delivery path's own laundered render."),
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
        "LAUNDERED+INTERNAL: owner-mention preview, the answers-card rows and "
        "the matching reads all launder every emitted identity through "
        "chat._dsan."),
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
    "registry.py": (
        "NOT-A-CHAT-ROW: every `from` read here is the DEPARTED half of a "
        "repoint/undo location transition, whose record's keys happen to be "
        "spelled from/to — a filesystem path, never a chat/meld sender. The "
        "three readers are _binding_history's chain validator "
        "(event.get('from') paired with event.get('to')), the undo target "
        "(transitions[-1]['from']) and _proven_legacy_stamps' historical-stamp "
        "census (event.get('from')). None can launder a display identity: the "
        "values are paths the registry itself wrote and validated as "
        "normalized absolutes, and none reaches a chat sink. Enforced, not "
        "merely asserted, by RegistryFromReadIsALocationTest."),
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
    "chat.py": 36,         # +1: _append tests dm only to decide whether the
                            # roster census applies; MATCHING-ONLY, never emitted.
                            # +1: append-time broadcast census passes the row
                            # internally; its from-key is never emitted here and
                            # the receipt renderer launders frozen seat names.
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
    "route.py": 2,        # the routing verb's header: the family the caller
                          # asked FROM, compared against the alias they typed
                          # so the resolution is visible. A family name, not a
                          # chat-row sender (see the consumer note above).
    "registry.py": 3,     # binding transitions: event.get("from") and
                          # transitions[-1]["from"], plus (+1, re-pinned
                          # deliberately per this section's own contract)
                          # _proven_legacy_stamps' event.get("from") — the
                          # historical-stamp census walks a repoint chain and
                          # offers the DEPARTED LOCATION (a path) beside the
                          # arrived one; it is a location transition, never a
                          # chat-row sender, and it reaches no chat sink.
                          # RegistryFromReadIsALocationTest is what proves that
                          # about the source instead of trusting this comment.
    "inject/_whisper.py": 5,  # _council_reach: suffix walk x3 + mid-meld peer
                              # match; +1 _is_review_streak's per-speaker read —
                              # MATCHING-ONLY (it counts which SIDES used review
                              # vocabulary and emits no name; the whisper line
                              # itself launders the peer via chat._dsan)
    "meld.py": 17,    # +1: say's pinned-role preflight reads the legacy `peer`
                      # fallback into an INTERNAL membership set; every identity
                      # in the refusal is laundered by _membership_mismatch_lines.
                      # +4: lifecycle replay validates peer, matches it against
                      # the raw peer set, builds the legacy peer list, and copies
                      # peer/peers/done_peers/spoke_peers into the durable journal
                      # projection. The first three are MATCHING-ONLY; the fourth
                      # is a durable KEY record governed by meld.py's own
                      # "launder the EMIT, keep the KEY raw" law. (was 12)
                      # +1: _floor_line's yielder/still-to-hear reads (task #21
                      # honest floor) — MATCHING-ONLY set filtering; all three
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
    "machine_senders.py": 1,   # owner_rail's owner-name test: MATCHING-ONLY,
                               # a bool, never an emitted name.
    # third wave: one site to seats_delivery. 22 -> 21, 0 -> 1, 96 -> 96.
    "seats_delivery.py": 1,
    # fourth wave: two sites to seats_stop_signals. 21 -> 19, 0 -> 2, 96 -> 96.
    "seats_stop_signals.py": 3,   # +1: _meld_open_with, the spiral block's
                                  # open-room detector (task/981). VERIFIED
                                  # BEFORE RE-PINNING, which is the whole point
                                  # of this arm: it reads peer/peers ONLY to
                                  # test membership and never emits them; the
                                  # one value that reaches a sink is `room`,
                                  # laundered AT THE READ via
                                  # _clip(_scrub(...)) exactly like its sibling
                                  # _melded_with. The reason strings interpolate
                                  # the CALLER's peer argument, not the file's.
    # fifth wave: ten sites to seats_ack — it is the ack ladder, so the
    # from-field IS its subject. 19 -> 9, 0 -> 10, 96 -> 96.
    "seats_ack.py": 11,   # +1 RELOCATED, not new: render_pending moved here
                          # from seats_cli's `cmd` dispatcher so it sits beside
                          # pending()/fault_lines(), which produce the states it
                          # renders. Its `it["to"]` read went with it. The census
                          # TOTAL is unchanged at 97 and seats_cli drops by the
                          # same 1 — a moved read, not an unreviewed sink.
    # seats.py IS GONE FROM THIS PIN and its absence is the finish line:
    # the facade reads a chat row's identity fields ZERO times now. Catchup's
    # five reads moved again from seats_cli to seats_catchup; the split changes
    # ownership, never the conserved total. A pin entry for a module that no
    # longer reads would vouch for call sites that do not exist.
    "seats_cli.py": 1,    # -1 AGAIN, same shape as render_pending above:
                          # render_catchup left for seats_catchup, and the
                          # `chat._dsan(m.get("from"))` that names each parked
                          # row's author went with it. The legend that says
                          # what parked/held/retry MEAN now sits beside the
                          # catchup() that decides them.
    "seats_catchup.py": 6,   # +1 RELOCATED, not new — the read above. The
                             # census TOTAL is unchanged and seats_cli drops
                             # by the same 1; the split moves ownership, never
                             # the conserved total.
    # seats_stop_guard.py IS GONE FROM THIS PIN (task 692), like seats.py above:
    # trimming the inbox block dropped its up-to-5 sample rows, and the ONE
    # from-field read that rendered a row's author (chat._dsan(r.get("from")))
    # went with them. The idle gate now reads a chat-row identity ZERO times —
    # a pin entry for a module that no longer reads would vouch for a call site
    # that does not exist. The reads once catalogued here (the ack/consume
    # ladder, catchup tallies, _spiral_gate/_melded_with's peer) live in
    # seats_ack / seats_cli / seats_stop_signals and are pinned there.
    "web:_owner_signal": 4,   # +1: the answers-card row's author
                              # (task/2364) — EMITTED to the owner's own
                              # console, and laundered at the same door as the
                              # mention preview beside it (chat._dsan), because
                              # a chat row's from-field is written by any seat
                              # on the fleet and this card is the one surface
                              # that renders another writer's bytes to him.
    "web:_room_seats": 1,
    "web:_chat_gen": 2,
    "web:_api_chat_react": 1,
    "web:_turn_about": 1,
    "web:_native_chat_pulse": 1,
}


# The re-pin above is a HUMAN judgment ("this new read is a location, not a
# sender"), and a count cannot carry a judgment. This predicate carries it: for
# the one module allowlisted NOT-A-CHAT-ROW on the grounds that its from-reads
# are repoint locations, it re-derives that claim from the source.


def _non_transition_from_reads(pkg=PKG, module="registry.py"):
    """[(lineno, text)] registry from-reads that are NOT a location transition.

    A repoint/undo transition record spells its two ends `from` and `to`, so the
    departed end is read through the same accessor shape a chat row's sender
    would be — which is why the scanner counts it. What distinguishes them is
    the COMPANION: a transition read sits in a function that also reads the
    arrived end (`to`) or subscripts the binding's `transitions` list. A chat-row
    sender read (a lone `row["from"]` feeding a render) has neither, and is
    reported here. Innermost enclosing function only — taking the outermost
    would let any `to` anywhere in a long function vouch for an unrelated read.
    """
    path = os.path.join(pkg, module)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    tree = ast.parse(text, filename=path)
    funcs = [n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    out = []
    for owner, lineno, line in _from_field_read_sites(pkg):
        if owner != module:
            continue
        enclosing = sorted((n for n in funcs if n.lineno <= lineno <= n.end_lineno),
                           key=lambda n: n.end_lineno - n.lineno)
        keys = set()
        for node in enclosing[:1]:
            for child in ast.walk(node):
                if isinstance(child, ast.Call) \
                        and isinstance(child.func, ast.Attribute) \
                        and child.func.attr in ("get", "pop", "setdefault") \
                        and child.args and isinstance(child.args[0], ast.Constant):
                    keys.add(child.args[0].value)
                elif isinstance(child, ast.Subscript) \
                        and isinstance(child.slice, ast.Constant):
                    keys.add(child.slice.value)
        if not keys & {"to", "transitions"}:
            out.append((lineno, line))
    return out


class RegistryFromReadIsALocationTest(unittest.TestCase):
    """registry.py's from-reads are LOCATIONS, proven from the source.

    The round-seven gate red was exactly this: a new `from` read entered
    registry.py and the count pin tripped, because a count cannot tell a
    repoint path from a sender name. Re-pinning restores green; this class is
    what makes the re-pin mean something, and it keeps meaning it for the next
    read site nobody has written yet."""

    def test_every_registry_from_read_is_a_location_transition(self):  # noqa: VACUOUS_ASSERTION — the same-observable positive is the
        # sibling arm below: test_a_planted_chat_row_sender_read_is_
        # reported drives THIS predicate to a NON-empty report on a
        # planted read, so an instrument that can only ever return []
        # fails there. This arm additionally asserts its own input (the
        # three known reads) so an empty census cannot satisfy it.
        # POSITIVE on the SHIPPED module — no fixture input at all, the real
        # helm/registry.py as it will run. The emptiness below is only worth
        # something if the instrument saw input, so assert the input first: the
        # census must find the three known transition reads in registry.py. An
        # empty census (a rotted regex, a moved module) fails HERE rather than
        # passing vacuously through the assertion after it.
        read = [(l, t) for o, l, t in _from_field_read_sites()
                if o == "registry.py"]
        self.assertEqual(
            3, len(read),
            "the census no longer sees registry.py's three transition reads, so "
            "the emptiness asserted below would prove nothing: %r" % (read,))
        self.assertEqual(
            [], _non_transition_from_reads(),
            "registry.py reads a `from` field that is not part of a location "
            "transition. It is allowlisted NOT-A-CHAT-ROW on the grounds that "
            "every such read is a repoint path; a read with no `to`/"
            "`transitions` companion contradicts that reason and must be "
            "re-justified (or laundered) before the pin is raised.")

    def test_a_planted_chat_row_sender_read_is_reported(self):
        # NEGATIVE on an otherwise-valid input: the REAL registry.py, unchanged
        # except for one lone `row["from"]` render — the precise shape the
        # allowlist's reason forbids, so the arm can only fail at the gate
        # under test.
        with tempfile.TemporaryDirectory() as tmp:
            pkg = os.path.join(tmp, "helm")
            os.mkdir(pkg)
            shutil.copy(os.path.join(PKG, "registry.py"),
                        os.path.join(pkg, "registry.py"))
            # CONTROL. Blast radius: this temporary copy only — no shipped
            # file, no process or store state is touched, so the control cannot
            # perturb its own subject. Without the plant the predicate reports
            # nothing, which is what proves the red below comes from the
            # planted line and not from the copy or the scanner. It is
            # guarded by its own positive: the copy must still PRESENT the
            # three transition reads, so "reports nothing" means "found them
            # and cleared them", never "read an empty file".
            self.assertEqual(
                3, len([1 for o, _, _ in _from_field_read_sites(pkg)
                        if o == "registry.py"]))
            self.assertEqual([], _non_transition_from_reads(pkg))
            with open(os.path.join(pkg, "registry.py"), "a",
                      encoding="utf-8") as fh:
                fh.write('\n\ndef _announce(row):\n'
                         '    return "repointed by %s" % row["from"]\n')
            planted = _non_transition_from_reads(pkg)
            self.assertEqual(1, len(planted), planted)
            self.assertIn('row["from"]', planted[0][1])


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


@_real_tree_once
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
        was the contract until a review showed laundering ALIASES: a
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


class RealTreeScannedOncePerProcessTest(unittest.TestCase):
    """The site scanners read helm/ once per process; a planted package is
    read on every call (task/3039).

    MEASURED BEFORE THE MEMO: 9 of 56 tests took 95% of this module, with
    3,491 `ast.parse` calls. `_roster_call_sites` read the real tree 3 times,
    `_from_field_read_sites` about 4 times and `_transport_projection_sites`
    twice, and helm/ does not change while one process runs.
    """

    _SCANS = ("_roster_call_sites", "_from_field_read_sites",
              "_transport_projection_sites", "_chat_name_read_sites")

    def _reads(self):
        """Count every source file this module opens during the case."""
        real_open, opened = open, []

        def counting(path, *a, **kw):
            if str(path).endswith(".py"):
                opened.append(path)
            return real_open(path, *a, **kw)

        patch = unittest.mock.patch("builtins.open", counting)
        patch.start()
        self.addCleanup(patch.stop)
        return opened

    def test_a_second_real_tree_scan_reads_no_file(self):  # noqa: VACUOUS_ASSERTION — the counter is proven live in this arm: a planted package scanned under the same patch must add its file
        module = sys.modules[__name__]
        first = [getattr(module, name)() for name in self._SCANS]
        pkg = tempfile.mkdtemp(prefix="helm-scan-control-")
        self.addCleanup(shutil.rmtree, pkg, True)
        with open(os.path.join(pkg, "planted.py"), "w", encoding="utf-8") as fh:
            fh.write("x = 1\n")
        opened = self._reads()
        second = [getattr(module, name)() for name in self._SCANS]
        by_second = list(opened)
        _roster_call_sites(pkg)
        self.assertTrue(all(first), "control: every scanner found real sites")
        self.assertEqual(second, first)
        self.assertEqual(len(opened), len(by_second) + 1,
                         "control: the counter sees a file a scan reads")
        self.assertEqual(by_second, [], "a second real-tree scan read helm/ again")

    def test_a_planted_package_is_scanned_every_time(self):
        """The must-miss, after the real tree is remembered."""
        _roster_call_sites()
        _from_field_read_sites()
        pkg = tempfile.mkdtemp(prefix="helm-scan-planted-")
        self.addCleanup(shutil.rmtree, pkg, True)
        path = os.path.join(pkg, "planted.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("from helm import seats\nrows = seats.roster()\n"
                     "who = row['from']\n")
        self.assertEqual([m for m, _n, _t in _roster_call_sites(pkg)],
                         ["planted.py"])
        self.assertEqual([m for m, _n, _t in _from_field_read_sites(pkg)],
                         ["planted.py"])
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("again = seats.roster()\nwho = row['from']\n")
        self.assertEqual(len(_roster_call_sites(pkg)), 2,
                         "the planted package was served a remembered scan")
        self.assertEqual(len(_from_field_read_sites(pkg)), 2)

    def test_a_caller_that_edits_a_scan_cannot_change_the_next(self):
        sites = _roster_call_sites()
        self.assertTrue(sites, "control: the real tree has roster sites")
        sites.append(("planted.py", 1, "planted"))
        projections = _transport_projection_sites()
        projections.add(("planted.py", "planted"))
        sites_again, projections_again = (_roster_call_sites(),
                                          _transport_projection_sites())
        self.assertTrue(sites_again and projections_again,
                        "control: the next scans answered")
        self.assertNotIn(("planted.py", 1, "planted"), sites_again)
        self.assertNotIn(("planted.py", "planted"), projections_again)


if __name__ == "__main__":
    unittest.main()
