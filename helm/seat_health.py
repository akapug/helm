"""Seat status, drift diagnostics, and proxy health for :mod:`helm.seat`."""
import glob
import json
import os
import re
import shlex
import shutil
import sys
import time

# ESTABLISHED, NOT INHERITED. helm.seat bulk-copies its whole globals() into
# every impl module (seat.py `_seed_impl_modules`), so this module's call sites
# resolved names it never imported — and only while helm.seat had been imported
# FIRST. `_status` calls seats_root() at its first line; a test that
# direct-imports seat_health under one-module-per-process raises NameError
# there, which is exactly how the isolation audit found it.
#
# The name is imported from its OWNER rather than pinned to a module ORDER: the
# fanout still overwrites it with the identical object, so behaviour under the
# seeded path is unchanged, and the unseeded path now works instead of raising.
# THE PATH FAMILY, IMPORTED FROM ITS OWNER RATHER THAN INHERITED.
# helm.seat bulk-copies its whole globals() into every impl module
# (seat.py `_seed_impl_modules`), so these resolved only after helm.seat
# had been imported. Under one-module-per-process a test that
# direct-imports seat_health raises NameError at the first use.
#
# ALL EIGHTEEN seat_paths names this module borrows are listed, not just
# the two the failing tests reached: the audit measured `seats_root` and
# the fab run then found `seat_dir` one call deeper, because my static
# read covered `_status`'s own body and not its call graph. The other
# sixteen are the same defect not yet exercised, and curing one at a time
# is how a class survives its own fix.
#
# The fanout still overwrites each with the identical object, so the
# seeded path is byte-for-byte unchanged; only the unseeded path differs.
# Nine further bare names are owned by OTHER modules and are deliberately
# left alone — a different family, reported rather than smuggled in here.
from .seat_paths import (CRED_ABSENT, CRED_EXPIRED, PROXY_PID_REUSED,
    PROXY_STALE, PROXY_UNKNOWN, PROXY_UNVERIFIABLE, _existing_instance_port,
    _instance_port, _pid_alive, _port_open, _proxy_bin, _proxy_home, _proxy_pid_record,
    _proxy_pid_verdict, _rfc3339, _running_pid, _running_pid_rec,
    proxy_drift, seat_dir, seats_root)  # noqa: F401
from .seat_proxy import _proxy_binary_probe
from . import pk, projscope


def _minted_instances(family):
    """[seat] every instance with its OWN minted proxy (config.yaml under
    instances/<seat>/) — the per-instance-proxy fleet, sorted numerically so
    codex-2 precedes codex-10."""
    root = os.path.join(seat_dir(family), "instances")
    out = []
    for name in (os.listdir(root) if os.path.isdir(root) else []):
        if os.path.exists(os.path.join(root, name, "config.yaml")):
            out.append(name)
    def _key(s):
        m = re.match(r"^%s-(\d+)$" % re.escape(family), s)
        return (0, int(m.group(1))) if m else (1, s)
    return sorted(out, key=_key)


def _minted_seats():
    """(family, seat) for every MINTED proxy — family seats with a config.yaml
    plus each per-instance proxy. The ONE enumeration doctor/--ensure/the CPU
    canary all walk, so no surface can silently see a different fleet."""
    for family in sorted(FAMILIES):
        if not os.path.exists(os.path.join(seat_dir(family), "config.yaml")):
            continue                      # never minted: nothing to supervise
        for s in [family] + _minted_instances(family):
            yield family, s


def _model_from_launch_text(text):
    """The model a minted launch.sh runs `claude --model` with, parsed out of
    the script's BYTES, or None when nothing in them records one.

    THE MINT IS NOT THE RUNNING PANE. `instance_launch_model` answers what the
    catalog DECLARES a seat takes; this answers what the launch.sh ON DISK
    would start a pane with, and the two differ exactly where an operator
    pinned a model (`seat launch <seat> --model gpt-5.3-codex-spark` —
    persisted in spawn.json and re-derived into every later re-mint by
    `_persisted_model`) or where the declaration changed and the assets were
    re-minted. Neither is a reading of the process running right now: the
    script is re-written on every add, launch and resume, so a pane started
    before the last mint can be executing a model no file on disk names. That
    is why this value is rendered as `minted` and never as `launched` — see
    `_launched_model_text` for the field that would answer the other question.

    THE ONLY PRODUCER READ IS THE SHIPPED ARTIFACT: `launch_line` ends the
    command with `--model <model>` and `_write_launch_assets` bakes that line
    into launch.sh. No live process is probed and no model is inferred — a
    model-less script is None, which renders UNKNOWN rather than borrowing the
    declaration, because a blank or a borrowed value would be this listing's
    own "a blank column reads as health" defect.
    Tokenised shell-aware (the same read `_homing_from_launch` does) so a
    quoted value and the generated comments cannot be misread, and scanned
    from the END because the `claude --model` pin is the command's last word.

    THE NESTED RE-SPLIT IS LOAD-BEARING, not defensive tidiness, and a reader
    without it answers None for every live seat: `_launch_owner` hands the
    whole launch line to the supervisor as ONE shell-quoted argument
    (`shlex.quote("exec <line> \\"$@\\"")`), so a single `shlex.split` of the
    script yields the entire command — `--model` included — as one token. The
    same shape `_homing_from_launch` re-splits for HELM_CHAT_ROOM.
    """
    try:
        tokens = shlex.split(text, comments=True)
    except ValueError:
        return None
    nested = []
    for token in tokens:
        if "--model" in token and token != "--model":
            try:
                nested.extend(shlex.split(token))
            except ValueError:
                pass
    tokens.extend(nested)
    for i in range(len(tokens) - 1, 0, -1):
        if tokens[i - 1] == "--model":
            return tokens[i]
    return None


def _minted_model(path):
    """`_model_from_launch_text` over one script path, or None when the path
    cannot be read at all. The WORD a column renders needs to tell those two
    Nones apart, so the column calls `_minted_model_text`, never this.

    THE READ FAILS IN TWO FAMILIES, NOT ONE. `open` in text mode raises OSError
    for a path it cannot reach and UnicodeDecodeError — a ValueError, NOT an
    OSError — for bytes it reached and could not decode. An OSError-only
    boundary lets the second one out of the field reader; see
    `_minted_model_text` for what that cost the listing."""
    try:
        with open(path) as f:
            text = f.read()
    except (OSError, ValueError):
        return None
    return _model_from_launch_text(text)


def _minted_model_text(path):
    """The minted column's WORD for one seat: a model name, `unminted`, or an
    UNKNOWN that names why — and those are three different facts.

    A FAILED LOOKUP IS NOT PROVEN ABSENCE. The round-2 reader rendered the
    never-launched word for every `os.path.exists` that came back False, and
    that predicate answers False for a seat helm could not look at: a parent
    directory with no search permission, an I/O error, a dangling symlink
    whose NAME is right there on disk. Saying "this seat was never launched"
    off a read that never reached the file is the same class of overclaim the
    launched/minted rename cures one field over. Absence is now exactly
    FileNotFoundError with nothing at the path (`os.path.lexists` False);
    every other OSError renders UNKNOWN carrying its error CLASS, so the
    operator's next move (fix the mode bits, fix the symlink) is on the screen.

    AN ABSENT launch.sh IS STILL A MEASURED ABSENCE, NOT AN UNKNOWN. A seat
    whose assets were never minted (orca-adopted, or added and never launched)
    has no script and therefore no minted model; helm read the disk and the
    answer was "there is nothing here". Rendering that as UNKNOWN spent this
    listing's scarcest word — "helm could not read that input" — on the one
    case helm read perfectly, and it collided with the CRED column next door:
    two arms in tests/test_seat_cred_state.py assert a healthy proxy-key
    seat's row carries no UNKNOWN at all.
    UNKNOWN IS KEPT FOR THE GENUINELY UNANSWERABLE: a launch.sh that EXISTS
    and yields no `--model` token — a command minted without a model pin — is
    an input helm looked at and could not answer from, which is exactly what
    UNKNOWN means everywhere else on this surface.
    """
    try:
        with open(path) as f:
            text = f.read()
    except FileNotFoundError:
        if os.path.lexists(path):
            # THE NAME IS THERE AND THE TARGET IS NOT: a dangling symlink.
            # ENOENT from the open says nothing about whether this seat was
            # ever launched — something IS minted at that path — so this is a
            # read helm could not complete, not an absence.
            return "UNKNOWN (dangling symlink)"
        return "unminted"
    except (OSError, ValueError) as exc:
        # BOTH FAILURE FAMILIES OF THE READ, AT THE READ. `open(path)` in text
        # mode decodes, so a launch.sh carrying one undecodable byte — an
        # owner-wrapped preset whose shell comment holds a raw 0xff — raises
        # UnicodeDecodeError, which is a ValueError and NOT an OSError. Under
        # the OSError-only boundary that exception left this field reader and
        # unwound `_seat_row`, which assembles the family line AND every
        # instance line before it returns: `_status` caught it, printed ROW
        # FAILED, and one seat's undecodable script erased the family row plus
        # every healthy sibling's liveness, cred and usability. The bad byte
        # is a fact about ONE minted field, so it is rendered as one
        # error-qualified UNKNOWN and the rest of the block survives.
        #
        # TEXT MODE PLUS ValueError, NOT bytes-then-decode: the read stays the
        # one `open(path)` every other arm here drives, and the class NAME
        # (`UnicodeDecodeError`, `PermissionError`) still reaches the screen,
        # so the operator's next move is on the row. A `errors="replace"` read
        # would be the other cure and it is the wrong one — it would silently
        # invent a model token out of mojibake rather than say helm could not
        # read the file.
        return "UNKNOWN (%s)" % type(exc).__name__
    return _model_from_launch_text(text) or "UNKNOWN"


def _launched_model_text(seat, row=None):
    """What the seat's RUNNING pane attested it launched on.

    THE ONLY RECORD OF A LIVE PANE'S IDENTITY is the roster's runtime row
    (`seats_runtime._runtime_metadata`, stamped through `write_roster`,
    `bind_lifecycle_runtime` and proxywatch's measured `stamp_proxy_runtime`).
    That validator now carries `model` beside `agent_harness`, `family` and
    `backend` (task/2655), and proxywatch's decoder carries the model the
    route already bound instead of collapsing it to a family — so a pane on a
    measured proxy route attests its MODEL and not only its FAMILY.

    THIS FUNCTION DID NOT CHANGE WHEN THAT HAPPENED, and that was the design:
    it always read `runtime["model"]` and answered UNKNOWN only because
    nothing ever stored one. UNKNOWN therefore still means exactly what it
    meant — this row carries no attested model — rather than "helm cannot
    have one", and it is still the honest answer for a native pane, an
    unverified runtime, or a row written before the field existed.

    THE `launched` COLUMN IS NOW RENDERABLE beside `minted` and is NOT
    rendered here: that is a listing decision with its own cost (UNKNOWN would
    land in the healthy proxy-key rows two cred arms guard), and it belongs to
    whoever changes the listing, not to this helper.
    NO LIVE-PROCESS DISCOVERY IS DONE HERE: inferring a pane's model from
    /proc or a tmux title would be inventing the very evidence this field says
    helm does not have.

    THE ROW IS HANDED IN AND NEVER RE-READ. The round-3 reader fetched
    `seats.roster()` itself when no row was passed, which made this display
    helper a SECOND roster consumer — exactly the launder-by-second-read that
    tests/test_display_launder_tripwire.py's call-site count-pin exists to
    stop: every caller on this surface (`_seat_row`, the listing) already
    holds the roster it folded once, and a display that re-reads can render a
    key no sink laundered and a row the rest of the line disagrees with. So
    the row is the CALLER's; a caller that has none supplies None, and then
    this function reads NOTHING and answers UNKNOWN — the same word it owes
    any input it could not answer from.
    """
    if not isinstance(row, dict) or row.get("runtime_verified") is not True:
        return "UNKNOWN"
    runtime = row.get("runtime")
    model = runtime.get("model") if isinstance(runtime, dict) else None
    return str(model) if model else "UNKNOWN"


def _proxy_live_text(family, seat=None):
    """One authenticated process record drives both liveness and drift text.

    THIS SURFACE MUST NOT SAY "down" FOR A LISTENING PROXY. Measured 2026-07-29:
    all three codex proxies read "proxy down" while pid 815805 held :8319 and
    answered in 0.9ms, because their pidfiles are LEGACY BARE-PID (no captured
    birth identity) and the fail-closed ladder correctly refused them. The
    refusal was right; rendering it as absence was not, and "down" routes a
    maintainer to respawn a process whose pidfile — not whose liveness — is the
    problem. `seat doctor --ensure` read the identical signal and said
    "alive but unverifiable, refusing to signal": same input, honest output.
    """
    # `_running_pid_rec` STAYS the primary seam. It is what every other caller
    # and test mocks, and moving the renderer off it silently broke three
    # integration tests whose patches stopped intercepting — the reason lookup
    # is an ADDITION on the negative path, never a replacement for the read.
    rec = _running_pid_rec(family, seat)
    pid = rec["pid"] if rec else None
    # THE READER'S PORT, not the admission answer. `_instance_port` refuses a
    # numbered seat whose derived port reaches the allocated project block
    # (`codex-183` -> 8500) so nothing NEW is minted there — but a `codex-183`
    # minted before that block existed is serving on 8500 this second, and every
    # `port %d` below would format a None and take the whole row down. A seat
    # whose own config exists always has an integer here.
    port = _existing_instance_port(family, seat)
    # ...AND WHEN EVEN THE READER HAS NO ANSWER, SAY SO RATHER THAN FORMAT IT.
    # The sentence above is true about a seat whose config exists; a seat with a
    # pidfile and no readable config port has None here, and every `%d` below
    # raised a TypeError about string formatting from inside a status renderer.
    # This is the sibling of `_ensure_row`'s guard, one function over: the two
    # readers ask the same question of the same seat and must not disagree about
    # whether the answer can be missing.
    shown = "port %d" % port if port is not None else \
        "port UNRESOLVED (no allocation entry, no config port)"
    answering = port is not None and _port_open(port)
    why = raw = None
    if not pid:
        _, why, raw = _proxy_pid_verdict(family, seat)
    if pid:
        live = "proxy UP pid %d %s%s" % (
            pid, shown, "" if answering else " (port not answering!)")
    elif why in (PROXY_UNVERIFIABLE, PROXY_PID_REUSED):
        # An OPEN PORT is positive evidence a proxy is serving, independent of
        # the pidfile — so it settles liveness even when identity is unknowable.
        # A closed port leaves both unknown, and UNKNOWN is the honest word.
        rawpid = (raw or {}).get("pid")
        # name the CAUSE, not the verdict slug — "(unverifiable)" restates the
        # word it follows and tells the reader nothing they can act on
        cause = ("legacy bare-pid, no birth identity"
                 if why == PROXY_UNVERIFIABLE else "pid reused since launch")
        live = ("proxy UP pid %s %s — UNVERIFIABLE pidfile (%s): re-mint "
                "the pidfile or check creds, do NOT respawn" % (rawpid, shown, cause)
                if answering else
                "proxy UNKNOWN pid %s %s — %s AND port silent"
                % (rawpid, shown, cause))
    else:
        live = "proxy down"
    # THE PROVIDER WALL RIDES EVERY PATH. "proxy UP" is a claim about the
    # LOCAL process; a seat whose provider is refusing it is not usable, and
    # the column said UP for three hours while codex was hard-walled. Appended
    # after the drift marker so both can show — they are different failures.
    live += upstream_phrase(family, seat, compact=True)
    status, detail = proxy_drift(family, seat, record=rec)
    if status == PROXY_STALE:
        return live + " ⚠ STALE", detail
    if status == PROXY_UNKNOWN:
        return live + " ⚠ drift UNKNOWN", "UNKNOWN — " + detail
    return live, None


_UNRESOLVED = "UNKNOWN — no catalog entry"


def _unresolvable_family_row(family, usability):
    """The roster block for a seat directory NO catalog entry resolves.

    WHY THIS IS A ROW AND NOT A RAISE. `_status` enumerates families from the
    FILESYSTEM while FAMILIES RESOLVES them — one fact, two sources of truth,
    and the directory is reliably FIRST: a seat dir is created by USE, a
    catalog entry arrives with a LANDED LANE. An unresolvable directory is
    therefore a normal, transient state, and it used to take the whole verb
    down: `_instance_port` raised KeyError mid-loop, so the rows already
    printed stayed on screen and every family sorting AFTER the unknown one
    silently vanished. Measured 2026-08-11 — `~/.helm/_global/seats/cursor`
    existed with no `cursor` in FAMILIES, so `helm seat list` printed codex,
    died, and never reached ds4pro/gemini/grok/kimi. A TRUNCATED ROSTER READS
    AS A COMPLETE ONE: nothing on screen says four seats are missing, so the
    reader concludes they do not exist.

    So the one unresolvable seat renders UNKNOWN and enumeration continues —
    the discipline `_status` already applies to a failed usability join, whose
    comment states the rule: a silently dropped fact "would leave a blank
    column that reads as health, which is the exact defect this exists to
    end." A DROPPED ROW is that same lie in a louder register, so the seat
    keeps its line and the line says what is wrong with it.

    Only the CATALOG-DERIVED fields go UNKNOWN. Usability is keyed by seat
    NAME and needs no catalog, so pane/turn/holding still report real
    measurements for a seat helm otherwise cannot resolve.
    """
    # ONE listdir for the instance census, for the reason the resolvable path
    # states: asked twice, two renders could disagree about which seats exist
    # mid-scan and the zip below would pair a verdict with the wrong line.
    # Instances are rendered rather than skipped so that every name `_status`
    # counted into the join gets a row back — a minted instance dropped here
    # would be the same silent absence this function exists to prevent.
    instances = _minted_instances(family)
    lines = ["%-8s %-38s %s" % (family, _UNRESOLVED, _UNRESOLVED)]
    for inst in instances:
        lines.append("  %-6s %-38s" % (inst, _UNRESOLVED))
    if usability is not None:
        from . import seat_usability
        interleaved = []
        for text, name in zip(lines, [family] + list(instances)):
            interleaved.append(text)
            interleaved.append(seat_usability.line(name, usability))
        lines = interleaved
    # the same words `_require_seat` uses for this condition, so a reader who
    # meets it on one verb recognises it on the other
    lines.append("  ⚠ %s: unknown family (have: %s) — the seat dir %s exists "
                 "but no catalog entry resolves it, so port/mode/cred cannot "
                 "be read. Land the family's FAMILIES entry, or remove the "
                 "directory."
                 % (family, ", ".join(sorted(FAMILIES)) or "none",
                    seat_dir(family)))
    return "\n".join(lines)


def _seat_row(family, usability=None):
    """The family's roster block. `usability` is the ONE fleet-wide join from
    `seat_usability.join()` — passed IN, never computed here, so N families
    cost one ledger fold and one process census rather than N of each."""
    # RESOLVABILITY IS SETTLED FIRST, in the one function that renders a
    # family. The `FAMILIES.get` below tolerates a miss on its own, but the
    # helpers this calls still index FAMILIES DIRECTLY (`_instance_port`), and
    # one of them raising mid-loop is what truncated the roster.
    if family not in FAMILIES:
        return _unresolvable_family_row(family, usability)
    d = seat_dir(family)
    fam = FAMILIES.get(family) or {}
    # One authority for "what cred does this seat have" (cred_state), so an
    # unreadable cred can never render here as a verdict about the cred.
    # PRECEDENCE PRESERVED from the pre-fix reader: a pooled cred file speaks
    # even for a proxy-key family (mode "proxy-key" bakes its key into
    # config.yaml, but a pooled json in the auth-dir is still what the proxy
    # hot-reloads, and reporting the baked key over it would hide a real cred).
    auth = os.path.join(d, "auth")
    state, detail, email = cred_state(auth)
    # The proxy-key gate is the PRE-FIX PREDICATE VERBATIM ("if creds:"), and it
    # has to be: for mode "proxy-key" the key is baked into config.yaml, so NO
    # pooled cred file is the normal, healthy state — whether the auth-dir is
    # empty or was never created at all. Gating on CRED_ABSENT alone regressed
    # this: the live ds4pro and kimi seats have no auth-dir, which is UNKNOWN
    # (correctly, for a family that reads creds from there), and they rendered a
    # do-NOT-respawn warning in place of "api-key cred". Caught by dogfooding
    # `helm seat doctor`, NOT by the unit test, whose fixture created the dir.
    if fam.get("mode") == "proxy-key" \
            and not glob.glob(os.path.join(auth, "*.json")):
        cred = "api-key cred (baked into config.yaml)"
    else:
        cred = "%s — %s" % (email, detail) if email else detail
    live, detail = _proxy_live_text(family)
    details = [(family, detail)] if detail else []
    # ONE listdir for the instance census — asked twice, two renders could
    # disagree about which seats exist mid-scan and the zip below would pair a
    # usability verdict with the wrong proxy line.
    instances = _minted_instances(family)
    # PROXY LINES FIRST, ASSEMBLED AS A LIST. They used to be concatenated into
    # one string; the usability verdicts have to be INTERLEAVED (each directly
    # under the seat it judges), and the codex capacity suffix must still land
    # on the last PROXY line exactly as before — both are one-liners on a list
    # and neither is expressible on a concatenated string.
    # THE LAUNCH MODEL IS A PER-SEAT FACT NOW, so it is a column and not a
    # family footnote: `instance_models` (seat_catalog) lets codex-4 launch
    # gpt-5.6-sol while codex-7 stays gpt-6-astra off one family entry, and an
    # operator who cannot SEE which is which has to read the catalog to know
    # what a seat is running. Rendered for every row from the same resolver
    # the generator and the launch line use, so the screen cannot disagree
    # with the config; an undeclared instance shows its family's model, which
    # is the default it is declared to take.
    # TWO COLUMNS, BECAUSE THEY ARE TWO FACTS. `declared` is the catalog
    # DEFAULT — what the next mint of this seat's assets derives its model
    # from — and `minted` is the model the launch.sh on disk would start a
    # pane with. One column carrying the declaration under a legend that said
    # "what THAT SEAT's pane launches on" was a FALSE CLAIM wherever an
    # operator pinned a model: the seat launched with `--model
    # gpt-5.3-codex-spark` (76000 window) kept reading sol on this screen, and
    # an operator allocating model-sized work off the roster would have sent
    # 320k-shaped work to a 76k pane.
    # NEITHER COLUMN IS `launched`, AND THAT IS MEASURED. Round 2 named the
    # disk column `launched`, which overclaims in the same direction one step
    # smaller: launch.sh is re-minted on every add, launch and resume, so it
    # proves what the NEXT spawn would use, never what the pane running right
    # now started with. The only shipped record of a live pane's own testimony
    # is the roster runtime row, whose validator carries agent_harness/family/
    # backend and no model at all — so `_launched_model_text` answers UNKNOWN
    # for every seat and this listing renders no such column (see that
    # function; the legend says so in the operator's words).
    def declared_model(seat):
        # UNKNOWN, NEVER A BLANK, for an entry that declares no model at all:
        # a blank column reads as health, which is the law this listing
        # already states for the usability join.
        from .seat_catalog import instance_launch_model
        return instance_launch_model(fam, seat) or "UNKNOWN"

    def minted_model(seat):
        # NEVER THE DECLARATION REPEATED — that would rebuild the false claim
        # one column over — and never UNKNOWN for a seat that simply has no
        # minted launch.sh: `unminted` is a measured absence, UNKNOWN is a
        # read helm could not complete. See `_minted_model_text`.
        from .seat_launch_assets import _instance_dir
        return _minted_model_text(
            os.path.join(_instance_dir(family, seat), "launch.sh"))

    lines = ["%-8s %-38s %-12s %-12s %s"
             % (family, live, declared_model(family), minted_model(family),
                cred)]
    # per-instance proxies: each minted instance reports its OWN proxy fate
    for inst in instances:
        ilive, idetail = _proxy_live_text(family, inst)
        # unpadded LAST column — the 38-wide liveness field already aligns it
        # under the family row's, and padding the end of the line would add
        # trailing blanks the codex capacity suffix then lands behind.
        lines.append("  %-6s %-38s %-12s %s"
                     % (inst, ilive, declared_model(inst),
                        minted_model(inst)))
        if idetail:
            details.append((inst, idetail))
    if family == "codex":   # slice 6: live-instance / pooled-capacity suffix
        try:
            from . import codexhomes, seats as _seats
            now = time.time()
            live_n = 0
            for s, r in _seats.roster().items():
                if s == "codex" or s.startswith("codex-"):
                    ls = _seats.last_seen(s, r)
                    if ls and now - ls < _seats.QUIET_S:
                        live_n += 1
            # cap "?" rather than "cap 0" when the pool would not enumerate:
            # this line is read as a measurement (task/2480 R5).
            cap = codexhomes.capacity()
            lines[-1] += "  [instances: %d live / cap %s]" % (
                live_n, "?" if cap.get("unknown") else cap["total"])
        except Exception:
            pass
    # THE USABILITY LINE — the answer to "can this seat take work right now",
    # under the proxy line whose "proxy UP" it qualifies. One seat read "proxy
    # UP ... 239h06m left" while 42 hours dark, and another read "proxy UP"
    # with its pane GONE; both facts live one line below now.
    if usability is not None:
        from . import seat_usability
        interleaved = []
        for text, name in zip(lines, [family] + list(instances)):
            interleaved.append(text)
            interleaved.append(seat_usability.line(name, usability))
        lines = interleaved
    row = "\n".join(lines)
    if details:
        row += "\n" + "\n".join("  ⚠ %s: %s" % item for item in details)
    return row


_POPULATION_NAMED = 6           # names printed before the remainder is counted


def _rostered_elsewhere(shown):
    """([seat], error) — rostered seats this listing does not enumerate.

    THE VERB'S NAME PROMISES A FLEET AND ITS BODY ENUMERATES A POOL.
    `_status` lists `os.listdir(seats_root())` plus each family's minted
    instances: the seats that have a PROXY DIRECTORY on this box. A seat
    without one is not a lesser seat, it is a different KIND of seat -- every
    native claude seat in this fleet is rostered, live, and has no directory
    here -- and it was rendering as nothing at all.

    A MISSING ROW MUST NEVER READ AS ABSENT. The per-row guard below already
    says so for one seat that fails to render ("a dropped row reads as 'no
    such seat', which is the worse of the two failures"); this applies the
    same rule to the POPULATION, where the omission is silent instead of
    loud and so is the more dangerous of the two.

    AN UNREADABLE ROSTER IS AN ERROR, NEVER AN EMPTY LIST. Returning () for
    "I could not look" would print the all-clear this function exists to
    stop, which is the defect wearing the cure's clothes.

    SO IT READS THE PATH STRICTLY AND DOES NOT CALL `seats.roster()`, which
    cannot raise: that is `pk.read_json(roster_path(), {})` on the NON-strict
    path, and non-strict swallows every exception and answers with the
    default. A corrupt, truncated or unreadable roster arrives there as `{}`
    and is indistinguishable from an empty one — so a guard built on catching
    its raise catches nothing and prints the all-clear for exactly the state
    it was written to refuse. `strict=True` is the mode pk documents as the
    one that "distinguish[es] missing from failed".

    AN ABSENT ROSTER IS NOT AN ERROR, and that asymmetry is deliberate rather
    than an oversight in the strict read: a box with no roster file yet has no
    rostered seats, which is a true empty and not a failed look. strict=True
    keeps that distinction — it returns the default for a missing file and
    raises only when the file exists and cannot be read.

    THE MATCH IS CASEFOLD-EXACT, NOT RAW. seats_common.canonical_keys states
    the relation and why it is the only correct one here: "SEAT IDENTITY IS
    CASEFOLD-EXACT ... a READER that indexes the mapping with a raw spelling
    asks a different question and gets a silent miss, which reads exactly
    like an absent seat." A raw membership test would name a seat whose
    roster key is `Codex` as missing from a pool holding `codex` — this
    note about false absences manufacturing one.
    """
    from . import pk
    from .seats_common import roster_path
    try:
        rows = pk.read_json(roster_path(), {}, strict=True) or {}
    except Exception as e:                  # noqa: BLE001
        return [], "%s: %s" % (e.__class__.__name__, e)
    have = {str(s).casefold() for s in shown}
    return [s for s in sorted(rows) if str(s).casefold() not in have], None


def _population_note(shown, families):
    """The disclosure line(s) for what this listing DID and DID NOT cover.

    `shown` is EVERY name rendered — families AND their minted instances —
    not the family list. Passing families alone would report each minted
    instance as omitted while it sits three lines up the screen, which is
    the same false-absence this note exists to prevent, pointed inward.
    """
    # INDENTED, AND THAT IS A CONTRACT RATHER THAN STYLE. This listing's
    # readers take any COLUMN-0 line to be a rendered seat row and read its
    # first token as the family name (tests/test_seat_roster.py _family_rows
    # says so: "instance, usability, warning, and legend lines are all
    # indented"). An unindented note beginning "helm seat list ..." minted a
    # phantom family called `helm` — this note about false rows inventing a
    # false row.
    one = len(families) == 1
    out = ["  helm seat list enumerates the PROXY POOL — %d seat director%s "
           "under %s plus %s minted instances (%d name%s above). It is "
           "not the fleet."
           % (len(families), "y" if one else "ies", seats_root(),
              "its" if one else "their", len(shown),
              "s"[:len(shown) != 1])]
    missing, err = _rostered_elsewhere(shown)
    if err is not None:
        out.append("  ⚠ how many rostered seats this listing OMITS is "
                   "UNKNOWN — the roster did not read (%s). Absence above "
                   "proves nothing until that reads." % err)
        return out
    if not missing:
        return out
    # LAUNDERED AT THE ONE DOOR THEY LEAVE BY. These are roster KEYS, and a
    # key is unvalidated at the join seam — a hostile HELM_CHAT_NAME carrying
    # ESC or bidi would reshape the operator's terminal from the most
    # prominent line this verb prints. `_seat_label` is the same law the two
    # other direct-roster-read surfaces already obey; laundering per-branch
    # instead would be a bet re-placed at every future caller.
    from .seats_common import _seat_label
    named = [_seat_label(s) for s in missing[:_POPULATION_NAMED]]
    rest = len(missing) - len(named)
    out.append("  ⚠ %d rostered seat%s NOT listed above, and their absence "
               "here is not evidence they do not exist: %s%s. `helm chat "
               "seats --all` is the roster."
               % (len(missing), "s"[:len(missing) != 1], ", ".join(named),
                  (" (+%d more)" % rest) if rest else ""))
    return out


def _status(args):
    """`helm seat list|status` — the whole render under ONE memo scope.

    THE PROMISE ONE LAYER DOWN WAS PAID TWICE. `seat_usability.join` says
    "FOUR reads total, whatever the fleet size", and it is right about ITSELF:
    it folds the dispatch ledger once and derives every seat from that one
    result. But the same pass also reaches `proxywatch.health`, which folds
    the SAME ledger again for its own idle derivation — two modules each
    correctly folding once per pass, in the same pass, neither able to see the
    other. Measured on the live fleet: 2,544 subprocess spawns for 786
    distinct (cwd, argv) questions, almost exactly two of everything.

    A SCOPE COLLAPSES THEM BECAUSE THE SECOND FOLD ASKS THE FIRST FOLD'S
    QUESTIONS, which is what `projscope.memo` is for and is a strictly
    cheaper cure than plumbing one fold's result through two module
    boundaries. It does not make the double fold go away — see the module
    that owns each read — it makes the second one free.

    THIS DOOR IS READ-ONLY AND THAT IS WHY THE SCOPE MAY SIT HERE. `list` and
    `status` are the only verbs that reach `_status`; the write verbs in this
    family (`--ensure`, cred-follow) have their own doors and keep the
    unmemoised reads projscope's contract gives them.
    """
    root = seats_root()
    fams = sorted(f for f in (os.listdir(root) if os.path.isdir(root) else [])
                  if os.path.isdir(os.path.join(root, f)))
    if not fams:
        print("helm seat: no seats yet — `helm seat add codex`")
        return 0
    with projscope.scope():
        return _status_render(fams)


def _status_render(fams):
    """The render itself, so the scope above is one line rather than an
    indentation change over sixty lines of load-bearing prose."""
    from . import seat_usability
    # ONE join for the WHOLE fleet — four reads total. Computed here and handed
    # down, because a per-family join would fold the dispatch ledger and walk
    # the host process table once per family.
    names = []
    for f in fams:
        names.append(f)
        names.extend(_minted_instances(f))
    try:
        usability = seat_usability.join(seats=names)
    except Exception as e:                  # noqa: BLE001
        # LOUD, AND STILL A ROSTER. Every reader of this join is wrapped, so a
        # raise here is structural — but a traceback in place of the roster
        # helps nobody, and a SILENTLY DROPPED join would leave a blank column
        # that reads as health, which is the exact defect this exists to end.
        # An empty map renders UNKNOWN in every field of every row.
        print("helm seat: the usability join FAILED (%s: %s) — every seat's "
              "turn/pane/wall/holding reads UNKNOWN below"
              % (e.__class__.__name__, e), file=sys.stderr)
        usability = {}
    # WHAT ACTUALLY REACHED THE SCREEN, which is not the same list as `names`.
    # `_seat_row` renders a family AND its minted instances together, so a
    # family whose row RAISES prints ROW FAILED and its instances never appear
    # — while `names` still holds them. Counting `names` would claim lines
    # that are not above, and naming a rendered seat as omitted (or an
    # unrendered one as present) is the same false absence this note exists to
    # end, one screen further in.
    rendered = []
    for f in fams:
        try:
            print(_seat_row(f, usability=usability))
            rendered.append(f)
            rendered.extend(_minted_instances(f))
        except Exception as e:              # noqa: BLE001
            # THE RULE THE JOIN ABOVE ALREADY STATES, APPLIED TO THE ROW LOOP
            # IT PROTECTS. That guard exists because "a traceback in place of
            # the roster helps nobody, and a SILENTLY DROPPED join would leave
            # a blank column that reads as health" — and then the loop beneath
            # it rendered rows unguarded, so any per-row raise still cost the
            # reader every OTHER seat. The reader who needs this surface most
            # is the one whose fleet is already broken.
            #
            # DELIBERATELY LAST-RESORT AND NOT A DIAGNOSIS. The uncatalogued
            # family that motivated this — a provisioned `cursor` seat dir
            # whose FAMILIES entry had not landed, which made `_instance_port`
            # raise KeyError and blanked the whole roster on 2026-08-12 — is
            # named properly at the RENDER seam by `_unresolvable_family_row`,
            # because port/mode/cred are UNKNOWABLE for such a family rather
            # than merely absent. This clause exists for the raise NOBODY has
            # classified yet, and it must stay a net rather than grow into a
            # second classifier: two places deciding what an unrenderable seat
            # MEANS is how the answers drift apart.
            #
            # Loud, per-row, never silent — a dropped row reads as "no such
            # seat", which is the worse of the two failures, and the exception
            # class and message both travel because a bare "failed" routes
            # nobody anywhere.
            print("%-8s ROW FAILED (%s: %s) — every OTHER seat is still "
                  "rendered; this row alone is unknown"
                  % (f, e.__class__.__name__, e))
    for line in _population_note(rendered, fams):
        print(line)
    print(seat_usability.legend())
    return 0


# ---------------------------------------------------------------------------
# config drift — the generator's promise vs the file a proxy is actually running
# ---------------------------------------------------------------------------
# WHY A CENSUS AND NOT A REGENERATION. A live config carries per-family truth a
# regeneration would have to re-derive (port, model, upstream alias, the 0600
# token), so this compares only the INVARIANTS both generators emit identically
# for every family, and leaves everything else alone.
#
# MEASURED 2026-07-29: ds4pro's config carried NO nonstream-keepalive-interval
# while kimi's — written in the same second, by the same generator path —
# carried 15. Neither had been re-minted; both had been hand-patched, and
# ds4pro was skipped. `_config_yaml`'s own docstring names the consequence: a
# long non-streaming request (a compaction's ~360k summarize is the longest one
# a session makes) sits silent while the upstream thinks, the proxy reaps the
# idle socket, and Claude Code receives an EMPTY HTTP 200. From the outside
# that is a seat that simply went quiet — which is what the owner had been
# reporting about ds4pro for a week, as unreliability.
#
# The docstring even predicted the shape: "the live family configs carry 15s by
# hand; the generator must emit it too or every re-mint silently strips the
# fix". The generator was fixed. Nothing ever checked the files that were never
# re-minted, so a hand-patch that missed one family stayed missed. A config
# nobody re-mints is a config nobody re-checks — this is the check.

_CONFIG_INVARIANTS = (
    ("nonstream-keepalive-interval", "15",
     "a long non-streaming pass (a compaction summarize) gets an EMPTY HTTP "
     "200 when the proxy reaps the idle socket — the seat goes silent"),
)


def _config_values(path):
    """{key: value} for the config's TOP-LEVEL scalars, comments stripped.

    Deliberately not a YAML parse: helm ships stdlib-only, the invariants are
    all top-level scalars, and a config that carries a token must never be
    round-tripped through a writer that could reformat it.
    """
    out = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for ln in f:
                if not ln[:1].strip() or ln.lstrip().startswith("#"):
                    continue          # indented => nested; '#' => comment
                key, sep, rest = ln.partition(":")
                if sep and key.strip() and " " not in key.strip():
                    out[key.strip()] = rest.split("#")[0].strip().strip('"')
    except OSError:
        return None
    return out


def config_drift(path):
    """[(key, want, got, why)] — generator invariants this config does not
    carry. `got` is None when the key is absent entirely, which is the shape
    that bit ds4pro. An unreadable config reports no drift rather than a false
    one: doctor must not turn a permissions problem into a config alarm."""
    vals = _config_values(path)
    if vals is None:
        return []
    return [(k, want, vals.get(k), why)
            for k, want, why in _CONFIG_INVARIANTS if vals.get(k) != want]


def _ordinal_suffix(n):
    """st/nd/rd/th for a 1-based position, teens included."""
    if 11 <= n % 100 <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def _config_drift_lines():
    """One line per proxy whose complete generated policy differs or is unreadable."""
    out = []
    for family, seat in _minted_seats():
        path = os.path.join(_proxy_home(family, seat), "config.yaml")
        if not os.path.exists(path):
            continue
        try:
            plan = proxy_config_plan(path, family, seat)
        except (IndexError, KeyError, OSError, TypeError, ValueError) as exc:
            out.append("config drift: %-9s desired state unreadable — %s"
                       % (seat, exc))
            continue
        if plan["changed"]:
            detail = plan["alias_drift"] or "generated proxy policy differs"
            out.append("config drift: %-9s %s" % (seat, detail))
        # AND EVERY PROVIDER THE GENERATOR COULD NOT READ, INDEPENDENTLY OF
        # `changed`. A block whose own-depth YAML is outside the admitted
        # grammar is skipped rather than refused, so nothing else on this path
        # says it exists -- and the case that most needs saying is the SETTLED
        # one: once the readable routes are repaired the plan reports no
        # change, and a real route that quietly stopped being part of the
        # desired state would then be invisible on the only surface that
        # renders a seat's config state. Reported by INDEX because a block this
        # generator cannot parse may not have a readable name either.
        for row in plan.get("opaque") or ():
            # BOTH NUMBERS, because one alone misleads somebody. The INDEX is
            # zero-based and is the identity the merge binds to; the ORDINAL is
            # what a person counting blocks in the file will say. Printing only
            # "#1" makes a reader and the code mean different blocks.
            #
            # AND THE NAME, NEVER THE RAW LINE. A provider whose first own key
            # is `api-key:` would otherwise print its credential here and into
            # every log that captures this output.
            out.append("config opaque: %-9s provider at index %d (%d%s in file "
                       "order) is not readable by this generator and was "
                       "SKIPPED — %s"
                       % (seat, row["index"], row["ordinal"],
                          _ordinal_suffix(row["ordinal"]),
                          "name %r" % row["name"] if row.get("name")
                          else "no readable name"))
    return out


def _alias_drift_lines():
    """One non-green line for every minted proxy whose exact alias policy drifts."""
    out = []
    for family, seat in _minted_seats():
        path = os.path.join(_proxy_home(family, seat), "config.yaml")
        if not os.path.exists(path):
            continue
        reason = proxy_alias_drift(path, family, seat)
        if reason:
            out.append("alias drift: %-9s %s" % (seat, reason))
    return out


def _doctor(args):
    b = _proxy_bin()
    binary_ok = False
    if b:
        binary_ok, why, output = _proxy_binary_probe(b)
        if binary_ok:
            v = output.strip().splitlines()
            print("proxy binary: %s (%s)" %
                  (b, v[0] if v else "version unknown"))
        else:
            print("proxy binary: %s (present, unusable: %s)" % (b, why))
    else:
        print("proxy binary: MISSING — install CLIProxyAPI to %s, e.g.\n"
              "  gh release download v7.2.88 --repo router-for-me/CLIProxyAPI "
              "--pattern 'CLIProxyAPI_*_linux_amd64.tar.gz'" % PROXY_BIN_DEFAULT)
    c = shutil.which("claude")
    print("claude binary: %s" % (c or "MISSING from PATH"))
    cstate, src, cdetail = codex_cred_state()
    print("codex cred: %s (newest valid, access token until %s)"
          % (src, _rfc3339(_cred_exp(src))) if src else "codex cred: " + cdetail)
    _status([])
    # proxy-CPU canary per live proxy — the struggling-backend leading
    # indicator (a DOWN proxy is the status rows'/--ensure's story, not ours)
    for family, seat in _minted_seats():
        pid = _running_pid(family, seat)
        if pid:
            cstate, pct, window, note = _cpu_canary(family, seat, pid)
            print("cpu canary: %-9s %-9s %s"
                  % (seat, cstate.upper(), _canary_text(cstate, pct, window, note)))
    drift = _config_drift_lines()
    for ln in drift:
        print(ln)
    try:      # proxy-seat context% + autocompact latch — read-only visibility
        from . import autocompact
        for ln in autocompact.report_lines():
            print(ln)
    except Exception as e:
        print("autocompact: report unavailable (%s)" % e)
    # EXIT STATUS IS A VERDICT TOO, and it was the last place the old fold
    # survived: gating on `not err` failed a healthy fully-pooled fleet (see
    # codex_cred_state). Only a REAL negative fails. CRED_UNKNOWN does not:
    # it is printed loudly with do-NOT guidance, exactly as `helm doctor` files
    # every unreadable case as WARN and reserves exit 1 for a proven FAIL —
    # an exit 1 here would tell a machine "this fleet's creds are broken" on
    # the strength of a probe that could not see them.
    return 0 if binary_ok and c and not drift \
        and cstate not in (CRED_EXPIRED, CRED_ABSENT) else 1


# A live proxy still binding its port at startup must never read as WEDGED —
# an adversarial MED: two back-to-back 0.5s connect probes with no grace
# let a just-launched proxy be SIGTERMed, and cron firing inside the boot window
# churns kill->respawn->kill. Age source = pidfile mtime: _up writes the pidfile
# atomically at spawn, so mtime ~= launch time and stays readable even when
# /proc is restricted. Env-tunable for slow hosts / tests.
_ENSURE_STARTUP_GRACE_S = float(os.environ.get("HELM_ENSURE_STARTUP_GRACE", "15"))


def _proxy_age_s(family, seat):
    """Seconds since this proxy's pidfile was written (~= launch time), or None
    when there is no pidfile to age. mtime is the portable birth proxy: it does
    not depend on /proc readability and _up stamps it at spawn."""
    try:
        return time.time() - os.path.getmtime(
            os.path.join(_proxy_home(family, seat), "proxy.pid"))
    except OSError:
        return None


# ---------------------------------------------------------------------------
# proxy-CPU canary — the leading indicator BEFORE a proxy goes silent
# ---------------------------------------------------------------------------
# Owner evidence (htop, 2026-07-23): cli-proxy-api pids at 152% and 90.6% CPU
# while healthy siblings idle at ~0% — a proxy pegged at SUSTAINED high CPU is
# a struggling/looping backend for that seat's model, and the precursor of the
# silent death doctor --ensure heals after the fact. The canary reads
# /proc/<pid>/stat utime+stime as a WINDOW, never a point: %CPU over the span
# since the stored prior sample (tmpfs, a cron cadence apart) when one exists,
# else a short in-process double-read — a single high reading never classifies.

_CPU_SAMPLE_MAX_AGE_S = 900   # a stored sample older than this is history,
                              # not a window — fall back to a fresh double-read


def _env_float(name, default):
    try:
        return float(home.env(name, default))
    except ValueError:
        return float(default)


def _proc_cpu_sample(pid):
    """One /proc reading for a pid: {jiffies, age_s, clk, ts} or None when
    /proc cannot be read (gone pid, no /proc, permission) — the caller must
    surface UNKNOWN, never OK (no false-absence). jiffies is cumulative
    utime+stime; age_s is process age, because startup/model-load bursts are
    normal and must not read as thrash."""
    try:
        with open("/proc/%d/stat" % int(pid)) as f:
            tail = f.read().rsplit(")", 1)[1].split()
        with open("/proc/uptime") as f:
            uptime = float(f.read().split()[0])
        clk = os.sysconf("SC_CLK_TCK") or 100
        return {"jiffies": int(tail[11]) + int(tail[12]),
                "age_s": max(0.0, uptime - int(tail[19]) / clk),
                "clk": clk, "ts": time.time()}
    except (OSError, ValueError, IndexError, TypeError):
        return None


def _cpu_sample_path(seat):
    """Where a seat's prior jiffies reading lives BETWEEN doctor runs — RAM
    (tmpfs) when the host has it: the sample is disposable derived state, not
    seat fate, and must not touch the proxy home. HELM_PROXY_CPU_DIR pins it
    (tests); losing it merely degrades to the double-read path."""
    base = home.env("PROXY_CPU_DIR")
    if not base:
        base = os.path.join("/dev/shm", "helm-cpu-canary-%d" % os.getuid()) \
            if os.path.isdir("/dev/shm") \
            else os.path.join(seats_root(), ".cpu-canary")
    os.makedirs(base, mode=0o700, exist_ok=True)
    return os.path.join(base, "%s.json" % seat)


def _cpu_canary(family, seat, pid):
    """Tri-state CPU verdict for a LIVE verified proxy pid: (state, pct,
    window_s, note), state "ok" | "thrashing" | "unknown". SUSTAINED beats
    spike: the %CPU window is the span since the stored prior reading when one
    exists for this pid (cron cadence = the real sustain), else a short
    double-read (HELM_PROXY_CPU_CANARY_WINDOW_S, default 1s). Thrash =
    >= HELM_PROXY_CPU_CANARY_PCT (default 80) over the window, UNLESS the
    process is younger than HELM_PROXY_CPU_CANARY_GRACE_S (default 60) —
    startup bursts are normal. Unreadable /proc is UNKNOWN, not OK."""
    now = _proc_cpu_sample(pid)
    if now is None:
        return ("unknown", None, None, "unreadable /proc/%s/stat" % pid)
    path = _cpu_sample_path(seat)
    prior = None
    try:
        with pk.open_regular(path) as f:
            rec = json.load(f)
        if rec.get("pid") == pid and \
                1.0 <= now["ts"] - rec.get("ts", 0) <= _CPU_SAMPLE_MAX_AGE_S:
            prior = rec
    except (OSError, ValueError):
        prior = None                     # no/corrupt store: double-read below
    if prior is None:
        # first sight of this pid (or a stale/foreign sample): a short
        # double-read gives a real window — a single reading never classifies.
        time.sleep(min(max(_env_float("PROXY_CPU_CANARY_WINDOW_S", "1.0"),
                           0.1), 10.0))
        second = _proc_cpu_sample(pid)
        if second is None:
            return ("unknown", None, None, "pid %s vanished mid-sample" % pid)
        prior, now = now, second
    try:
        with open(path, "w") as f:
            json.dump({"pid": pid, "jiffies": now["jiffies"],
                       "ts": now["ts"]}, f)
    except OSError:
        pass          # losing the store degrades to double-read, never crashes
    window = now["ts"] - prior["ts"]
    if window <= 0:
        return ("unknown", None, None, "non-positive sample window (clock skew)")
    pct = max(0.0, now["jiffies"] - prior["jiffies"]) / now["clk"] / window * 100
    threshold = _env_float("PROXY_CPU_CANARY_PCT", "80")
    if pct < threshold:
        return ("ok", pct, window, "")
    grace = _env_float("PROXY_CPU_CANARY_GRACE_S", "60")
    if now["age_s"] < grace:
        return ("ok", pct, window, "startup burst — %.0fs old, grace %.0fs"
                % (now["age_s"], grace))
    return ("thrashing", pct, window, ">=%.0f%% threshold" % threshold)


def _canary_text(cstate, pct, window, note):
    """One human line for a canary verdict — shared by doctor and --ensure so
    the two surfaces can never describe the same proxy differently."""
    if cstate == "thrashing":
        return ("cpu %.0f%% sustained %.0fs (%s) — backend struggling"
                % (pct, window, note))
    if cstate == "unknown":
        return "cpu UNKNOWN (%s)" % note
    return "cpu %.0f%% over %.0fs%s" % (pct, window,
                                        " (%s)" % note if note else "")


def _ensure_quiet_heartbeat_path():
    """Disposable cadence state for the quiet cron surface."""
    base = home.env("ENSURE_QUIET_HEARTBEAT_DIR")
    if not base:
        base = os.path.join("/dev/shm", "helm-ensure-heartbeat-%d" % os.getuid()) \
            if os.path.isdir("/dev/shm") \
            else os.path.join(seats_root(), ".ensure-heartbeat")
    return os.path.join(base, "doctor-ensure.json")


def _ensure_quiet_heartbeat(total):
    """Publish one bounded proof-of-run line, then latch its cadence."""
    now = time.time()
    interval = max(0.0, _env_float("ENSURE_QUIET_HEARTBEAT_S", "3600"))
    path = _ensure_quiet_heartbeat_path()
    try:
        with pk.open_regular(path) as f:
            last = float(json.load(f).get("ts"))
    except (AttributeError, OSError, TypeError, ValueError):
        last = None
    age = None if last is None else now - last
    if age is not None and 0 <= age < interval:
        return False

    # Publication precedes the latch: if stdout fails, the next cron run must
    # retry rather than trusting a heartbeat that never reached the log.
    print("helm seat doctor --ensure: HEARTBEAT — %d proxy row(s) HEALTHY"
          % total)
    tmp = path + ".%d.tmp" % os.getpid()
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(tmp, "w") as f:
            json.dump({"v": 1, "ts": now}, f, sort_keys=True)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        print("helm seat doctor --ensure: heartbeat state not recorded: %s; "
              "next run repeats" % exc, file=sys.stderr)
    return True


def _ensure_row(family, seat):
    """One proxy's supervise-verdict: (label, state, detail). state is
    "healthy" | "respawned" | "unknown". The reconciler's whole job is to make
    every row provably one of the first two; a row it cannot prove is UNKNOWN,
    never a silent down/up (the fleet-truth fail-closed law)."""
    label = family if seat == family else seat
    port = _existing_instance_port(family, seat)   # reader, not admission
    if port is None:
        # UNRESOLVED IS UNKNOWN, and it is answered BEFORE the first `%d`. The
        # reader's accessor is the strongest authority there is about a seat that
        # exists, and it still answers None for a project instance with no ledger
        # entry and no readable config of its own. Every branch below formats that
        # answer with `%d` and probes it with `_port_open`, so without this guard
        # the row is a TypeError raised from inside the supervise loop — one
        # unresolvable seat taking down the verdict for every other seat in the
        # sweep. An honest UNKNOWN is the fail-closed law this function opens
        # with: no signal, no respawn, and a sentence naming the two files.
        return (label, "unknown",
                "endpoint unresolved — neither %s nor %s names a port, so helm "
                "cannot probe or respawn this proxy; re-mint the instance"
                % (_instance_ports_path(),
                   os.path.join(_proxy_home(family, seat), "config.yaml")))
    rec = _proxy_pid_record(family, seat)
    live = _running_pid(family, seat)
    # The discriminant is the LIVENESS of the recorded pid, not record-presence:
    #  - dead recorded pid   -> a STALE pidfile of a crashed proxy (the silent-
    #    starvation case the watchdog exists to heal). Fall through to respawn;
    #    _up's empty-check reads _running_pid (None for a corpse) and overwrites.
    #  - ALIVE recorded pid but _running_pid None -> identity verification FAILED
    #    on a live process: a REUSED pid now owned by a stranger (never signal)
    #    or a legacy bare-pid proxy (running but unverifiable). Both UNKNOWN —
    #    refuse to signal and refuse to respawn over a live foreign listener.
    if rec and not live and _pid_alive(rec["pid"]):
        return (label, "unknown",
                "pidfile pid %d alive but unverifiable (reused or legacy "
                "bare-pid) — refusing to signal or respawn over it" % rec["pid"])
    if live and _port_open(port):
        path = os.path.join(_proxy_home(family, seat), "config.yaml")
        try:
            plan = proxy_config_plan(path, family, seat)
        except (IndexError, KeyError, OSError, TypeError, ValueError) as exc:
            return (label, "unknown", "config desired state unreadable — %s" % exc)
        process_drift, _detail = proxy_drift(family, seat, record=rec)
        if plan["changed"] or process_drift != PROXY_CURRENT:
            rc = _up(family, quiet=True, seat=seat)
            pid = _running_pid(family, seat)
            if rc == 0 and pid and _port_open(port):
                return (label, "respawned",
                        "pid %d port %d after desired-state reconciliation" %
                        (pid, port))
            return (label, "unknown",
                    "desired-state reconciliation failed (rc %d)" % rc)
        return (label, "healthy", "pid %d port %d" % (live, port))
    # down (no live pid / stale record) or wedged (live pid, port not answering).
    if live and not _port_open(port):
        # STARTUP GRACE: a YOUNG non-answering proxy is STARTING, not wedged —
        # never SIGTERM it. Surface as unknown (still binding) and leave it for
        # the next cron cycle; only a proxy old enough to have bound AND still
        # failing the probe is truly wedged.
        age = _proxy_age_s(family, seat)
        if age is not None and age < _ENSURE_STARTUP_GRACE_S:
            return (label, "unknown",
                    "pid %d launched %.0fs ago, port %d not answering yet — "
                    "STARTING (grace %.0fs), not wedged; left for next cycle"
                    % (live, age, port, _ENSURE_STARTUP_GRACE_S))
        # wedged: a live verified process past its grace and still not serving.
        # Signal it away, then respawn.
        _down(family, seat)
    rc = _up(family, quiet=True, seat=seat)
    if rc != 0:
        # concurrent-_up loser race (LOW): a seat launching in the same
        # instant wins the flock, our _up reads 'already running' (rc 1) — that
        # is not a failure, the row is now HEALTHY under the winner. Re-probe
        # before crying UNKNOWN.
        pid = _running_pid(family, seat)
        if pid and _port_open(port):
            return (label, "healthy",
                    "pid %d port %d (a concurrent starter won the race)" %
                    (pid, port))
        return (label, "unknown", "respawn failed (rc %d); see proxy.log" % rc)
    pid = _running_pid(family, seat)
    if pid and _port_open(port):
        return (label, "respawned", "pid %d port %d" % (pid, port))
    return (label, "unknown", "post-respawn probe could not prove healthy")


def _codexhomes():
    """The module that owns pool-side codex auth — imported lazily, because
    `seat doctor` must keep working on a host that has no codex pool at
    all."""
    from . import codexhomes
    return codexhomes


def _cred_follow_pass():
    """Run the orca cred-follow rung for this supervisory pass, or None when
    it could not run at all. NEVER raises into the walk: the watchdog's job is
    proxy liveness, and a credential rung that broke must not take the
    liveness rows down with it (the same law `_watched_seats` states one
    module over)."""
    try:
        return _codexhomes().cred_follow(apply=True)
    except Exception as e:                  # noqa: BLE001 — a watchdog never raises
        print("helm seat doctor --ensure: the orca cred-follow rung failed "
              "(%s: %s) — proxy liveness below is unaffected"
              % (e.__class__.__name__, e), file=sys.stderr)
        return None


def _ensure(args):
    """doctor --ensure: supervise every minted family+instance proxy. Reuse the
    landed ownership primitives — never a second spawn path. A healthy row
    also runs the proxy-CPU canary: a pegged proxy is a struggling backend
    BEFORE it goes silent (the leading indicator; the respawn is the trailing
    one). rc 0 all proven ok; rc 1 WARN — a THRASHING or cpu-UNKNOWN canary on
    an otherwise-live proxy; rc 2 when any liveness row is UNKNOWN (a row the
    watchdog could not prove), so a cron line can page on 2 alone. --json
    emits the full machine-read rows; --quiet emits only non-healthy/action
    rows, with one periodic heartbeat when every enumerated row is healthy.
    An empty enumeration is one UNKNOWN in every contract, never success.

    The pass also runs the ORCA CRED-FOLLOW rung (`helm seat cred-follow`)
    with apply: a proxy that is up and healthy on a credential the owner has
    already switched away from in orca is exactly the fault the owner
    reported, and this walk is the supervisor that is already running. Its
    verdict RIDES the output and never moves `rc` — a host with no orca must
    not turn the proxy watchdog red, and the rung refuses nothing."""
    as_json = "--json" in args
    quiet = "--quiet" in args
    if as_json and quiet:
        print("helm seat doctor --ensure: --json and --quiet are distinct output "
              "contracts; choose one", file=sys.stderr)
        return 2
    # THE SUSPEND RUNG RIDES FIRST, and it is a detection before it is an
    # action: CLOCK_BOOTTIME minus CLOCK_MONOTONIC grows by exactly the sleep,
    # so a growth past the threshold says "resumed since the last pass" and
    # the sweep bounces every live family proxy (agents keep their panes,
    # context and beacons; one in-flight request fails and the client
    # retries) and kicks one detached proxywatch pass. THE FAILURE MODE IT
    # REMOVES (task/2721): a long suspend leaves every proxy serving dead
    # keep-alive connections, the proxywatch pass blocks on the first hung
    # canary, and the fleet cannot mint until the proxies are bounced. It
    # never moves `rc` — a host that cannot read its
    # clocks must not turn the watchdog red, and a first run only records.
    # THE RUNG USES THE WALL CLOCK DIRECTLY, never this module's `time`
    # alias: tests patch `seat_health.time.time` with a scripted cadence for
    # the heartbeat, and a wall-clock read through that alias CONSUMES the
    # script — measured: the cadence arm's third scripted value was spent by
    # the rung's detect() before the heartbeat read its own. `time.time_ns`
    # is what a clock read that must not share a scripted clock looks like;
    # the suspend detector only ever compares differences, so ns is fine.
    from . import suspend as _suspend
    for _line in _suspend.resume_rung(quiet=True, _now=time.time_ns() / 1e9):
        print(_line)
    # --json carries the rung in its payload below, always. The HUMAN surface
    # prints it only when it imported or found a fault (cred_follow_noteworthy
    # carries that policy and the reason): a watchdog that narrates its steady
    # state on every run is a watchdog nobody reads.
    follow = _cred_follow_pass()
    if not as_json and follow is not None \
            and _codexhomes().cred_follow_noteworthy(follow):
        for line in _codexhomes().cred_follow_lines(follow):
            print(line)
    unknown = thrash = cpu_unknown = total = visible = 0
    rows = []
    for family, seat in _minted_seats():
        total += 1
        label, state, detail = _ensure_row(family, seat)
        cpu = None
        if state == "unknown":
            unknown += 1
        elif state == "healthy":
            # canary only on a proven-live row: DOWN just respawned (its own
            # tri-state arm), and a fresh respawn is inside its startup burst
            # by definition. A pid that vanished between the row's probe and
            # ours is UNKNOWN, never OK (no false-absence).
            pid = _running_pid(family, seat)
            cpu = _cpu_canary(family, seat, pid) if pid else \
                ("unknown", None, None, "pid vanished between probes")
        shown = state
        if cpu is not None:
            cstate = cpu[0]
            if cstate == "thrashing":
                thrash += 1
                shown = "thrashing"       # the tri-state's middle arm, surfaced
            elif cstate == "unknown":
                cpu_unknown += 1
            detail += " — " + _canary_text(*cpu)
        if as_json:
            rows.append({"seat": label, "family": family, "state": state,
                         "shown": shown, "detail": detail,
                         "cpu": None if cpu is None else
                         {"state": cpu[0], "pct": cpu[1],
                          "window_s": cpu[2], "note": cpu[3]}})
        elif not quiet or state != "healthy" or cpu is None or cpu[0] != "ok":
            visible += 1
            print("%-10s %-9s %s" % (label, shown.upper(), detail))
    if not total:
        unknown += 1
        detail = "no minted proxy rows were enumerated"
        if as_json:
            rows.append({"seat": None, "family": None, "state": "unknown",
                         "shown": "unknown", "detail": detail, "cpu": None})
        elif quiet:
            print("helm seat doctor --ensure: HEARTBEAT UNKNOWN — " + detail)
        else:
            print("helm seat doctor --ensure: UNKNOWN — " + detail,
                  file=sys.stderr)
    elif quiet and not visible:
        _ensure_quiet_heartbeat(total)
    rc = 2 if unknown else (1 if thrash or cpu_unknown else 0)
    if as_json:
        print(json.dumps({"rows": rows, "unknown": unknown,
                          "thrashing": thrash, "cpu_unknown": cpu_unknown,
                          "cred_follow": follow, "rc": rc},
                         indent=2, sort_keys=True))
    if unknown:
        print("helm seat doctor --ensure: %d UNKNOWN row(s) — a proxy the "
              "watchdog could not prove healthy; investigate" % unknown,
              file=sys.stderr)
    elif thrash or cpu_unknown:
        print("helm seat doctor --ensure: WARN — %d THRASHING / %d cpu-UNKNOWN "
              "row(s); a pegged proxy is a struggling backend (the leading "
              "indicator before silent death)" % (thrash, cpu_unknown),
              file=sys.stderr)
    return rc
