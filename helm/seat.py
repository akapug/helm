#!/usr/bin/env python3
"""helm seat — multimodel seats for the claude-code harness.

A seat gives a NON-Claude model family the full claude-code harness (hooks,
skills, subagents) by pointing one isolated claude invocation at a local
Anthropic-wire proxy (CLIProxyAPI) that authenticates to the family's own
subscription OAuth. Proven live 2026-07-18 (see the claudex seat eval): codex
gpt-5.6-sol passed plain-prompt, tool round-trip, and subagent-spawn legs.

Family table is data: "proxy" families (codex/OpenAI OAuth) need CLIProxyAPI;
first-party-Anthropic-compatible families (kimi/glm/deepseek) need only a
base-url + key and slot in without a proxy — same seat dir, same launch shape.

FLEET HOOKS + IDENTITY: a seat is a first-class Claude Code launch surface.
launch_line exports HELM_CHAT_NAME=<family>, so the SessionStart join hook
registers the seat in the roster under its family name ('codex'/'kimi'/…) —
@-mentions of that name and owner posts then deliver to it between tool calls. The full
hook contract lives in the seat's claude/ config dir; `helm hooks install`
wires it there (hooks.py's SEAT_SPECS) and `helm hooks status` reports seat
coverage. The same launch line wires dregg-native client signing:
HELM_CELL_BIN=<absolute signer path> + per-seat HELM_CELL_PROFILE/DREGG_PROFILE,
so each family writes cave turns as its own stable cell instead of inheriting the
owner's profile. THE LAUNCH LINE EXPORTS AN ABSOLUTE PATH (DREGG_SIGNER_DEFAULT),
never a bare name, and that is deliberate: a bare name is resolved by
cell._resolve through shutil.which, so it is PATH-DEPENDENT, and the launched
process's PATH is not the one that verified the binary. Measured 2026-08-04:
with the signer's dir on PATH both forms give usable:True; with PATH stripped to
/usr/bin:/bin the bare name gives _resolve None / usable:FALSE while the
absolute still works. Exporting the RESOLVED path is what makes signing survive
a PATH the launcher does not control. AN OPERATOR SETTING IT BY HAND MAY STILL
USE A BARE NAME — cell._resolve takes either a slash-free name through PATH or a
path as-is — and that is the better AUTHORING form because a name PATH already
knows survives a prefix change. This line documents what the launcher does; it
is not a template to copy verbatim into a shell whose PATH differs.
A seat already running an old session must be relaunched
(a fresh `helm seat launch`) to pick up the identity, signer, and hooks.

Seat dir (~/.helm/_global/seats/<family>/, 0700):
  config.yaml   proxy config (0600 — carries the per-seat proxy token)
  auth/         proxy auth-dir with the TRANSLATED cred (0600). The proxy
                refreshes tokens into THIS copy only; the source cred home
                under ~/.codex-homes is read-only to helm, forever.
  token         the random per-seat proxy token (0600)
  launch.sh     the pasteable/executable launch preset (0700)
  claude/       the seat's isolated CLAUDE_CONFIG_DIR
  smoke-claude/ throwaway CLAUDE_CONFIG_DIR, recreated per smoke run
  proxy.pid / proxy.log

THE SCRUB GUARD (contract — mechanical, not advisory):
A CLAUDE-model seat must never inherit proxy env. Any helm code path that
launches, prints, or mints a `claude` invocation destined for a Claude
Max-OAuth seat MUST compose one of:
  scrub_env(env)  -> copy of env with SCRUB_VARS removed (subprocess launches)
  scrub_prefix()  -> "env -u VAR ..." string prefix (printed shell commands)
so ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY can never
bleed from a proxied seat's shell into a Max-OAuth seat. Known integration
points for the integrator: sessions.resume_command(), transcripts.make_cmd().

THE CHILD-STAMP GUARD (same register, opposite direction): every minted
launch strips CHILD_STAMP_VARS (CLAUDE_CODE_CHILD_SESSION + the inherited
SID/bridge id) — a seat that inherits them runs as a subprocess child with
transcript persistence silently OFF (bug-class
child-stamp-kills-seat-persistence).

HARD LAWS:
  - ANTHROPIC_API_KEY is never written anywhere by this module; launch lines
    actively unset it (env -u).
  - Source auth.json files are read + translated only — never modified.
  - Token-bearing files exist only inside the seat dir, 0600 from creation.
  - No browser logins: an expired/absent source cred prints the human's
    unblock line and exits 1.

Import-safe, stdlib-only.
"""


def _family_owner_aliases_are_unique(table=None):
    """Empty when no two families answer to the same owner word; else why.

    Cross-filing one family's sentence onto another is precisely what the
    name arm exists to stop, and until now nothing would have noticed two
    families claiming the same word — the arm would simply have admitted the
    quote for both. Asserted at import beside the port-base check, because a
    collision introduced by a NEW family is exactly the case no existing test
    is looking at."""
    table = FAMILIES if table is None else table
    owners = {}
    for name, fam in sorted(table.items()):
        for alias in sorted(_family_owner_aliases(name, fam)):
            owners.setdefault(alias, []).append(name)
    clashes = ["%r claimed by %s" % (alias, "+".join(names))
               for alias, names in sorted(owners.items()) if len(names) > 1]
    if clashes:
        return ("owner-statement aliases must identify ONE family; a shared "
                "word lets a sentence about one model back a pin on another: "
                + "; ".join(clashes))
    return ""


def _unbacked_window_reason(table=None):
    """Empty when every pinned context window is backed; else why it is not.

    THE TWO DIRECTIONS ARE NOT SYMMETRIC AND THIS FUNCTION MUST NOT BE READ AS
    IF THEY WERE. Understating a window costs one early compaction and is
    RECOVERABLE. Overstating it sails the seat into a 400 "input exceeds the
    context window" that in-band compaction CANNOT escape, because /compact
    replays the same oversized transcript (codex wedged at 369,663 tokens
    2026-07-30; ds4pro hard-down 2026-07-29 with /compact itself 400ing) —
    the wedge helm/watchdog.py exists to catch. So the MEASURED-DISPROOF arms
    are the safety arms, and omitting a pin entirely is always allowed and
    always safe.

    ORDER IS THE DESIGN. Measured disproofs run FIRST and bind every grade,
    including the owner's: a ceiling the fleet has actually crashed into, an
    endpoint's own context_length, and a floor a live seat was watched holding.
    A statement — from an owner or anyone — never overrules a reading. Only
    after those does the function ask which grade BACKS the pin, and there the
    arms are COHERENCE, not safety:
      * a pin with none of WINDOW_BACKINGS is a bare assertion, the exact class
        the "no guessed window" rule was protecting against;
      * a floor at or below CC's own assumed 200k earns nothing, because an
        unpinned family already resolves to 200k — so such a reading has
        disproven nothing and cannot back a pin;
      * a floor-backed pin more than OBSERVED_FLOOR_HEADROOM above its floor is
        extrapolation. This arm binds THE FLOOR GRADE ONLY — see the note on
        the constant for why an owner statement is not an extrapolation and is
        not ratio-checked.

    SCOPE IS EVERY FAMILY THAT PINS, not just the proxy-oauth ones. It read
    `if fam.get("mode") != "proxy-oauth": continue` until 2026-08-03, on the
    reasoning that other modes had a probe channel of their own; ds4pro (mode
    proxy-key) then took a pin with no probe channel at all, and under the old
    scope the guard would simply have SKIPPED it. A guard a new pin can step
    around by being the wrong mode is not a guard.

    `table` exists so a test can drive bogus tables through the real predicate
    without mutating the live FAMILIES; production always passes nothing.
    """
    for name, fam in (table if table is not None else FAMILIES).items():
        win = fam.get("max_context")
        if not win:
            continue          # the honest omission — CC's 200k default, safe
        floor = fam.get("observed_context_floor") or 0
        ceiling = fam.get("observed_context_ceiling") or 0
        probed = fam.get("probed_context_length") or 0
        owner = fam.get("owner_stated_window")

        # --- measured disproofs, which outrank every claim including the
        # owner's. These are the fatal-direction arms.
        if ceiling and win >= ceiling:
            return ("%s pins max_context=%d at or above its "
                    "observed_context_ceiling %d — a request that size was "
                    "MEASURED to 400 'input exceeds the context window' and "
                    "/compact could not escape it. A measured ceiling "
                    "outranks every other grade, the owner's included"
                    % (name, win, ceiling))
        if probed and win > probed:
            return ("%s pins max_context=%d above the probed_context_length "
                    "%d its own endpoint reports" % (name, win, probed))
        if win < floor:
            return ("%s pins max_context=%d below its own observed floor %d"
                    % (name, win, floor))

        # --- every grade the entry records must be internally coherent,
        # whichever one ends up backing the pin. An owner statement is checked
        # wherever it appears, so a bogus quote can never ride along quietly
        # behind a measurement.
        if owner is not None:
            bad = _owner_statement_reason(name, win, owner, fam)
            if bad:
                return bad

        # --- and at least one grade must SUFFICE. MEASURED GRADES BACK FIRST:
        # where a family holds both a reading and a statement (codex holds a
        # 400 point AND the owner's "320k is fine for codex"), the READING is
        # what backs the number and the statement is corroboration. The owner
        # grade is the backing only where no reading reaches — which, in this
        # table, is gemini alone.
        if probed or ceiling:
            continue          # bounded above by a reading, which is the pin
        if owner is not None:
            continue          # the owner's own number, quoted and dated
        if floor:
            if floor <= _CC_ASSUMED_WINDOW_MIRROR:
                return ("%s records observed_context_floor=%d, at or below "
                        "CC's assumed %d — an unpinned family already gets "
                        "that, so the reading disproves nothing and backs no "
                        "pin" % (name, floor, _CC_ASSUMED_WINDOW_MIRROR))
            if win > floor * OBSERVED_FLOOR_HEADROOM:
                return ("%s pins max_context=%d, more than %sx its observed "
                        "floor %d — that is extrapolation, not observation, "
                        "and overstating a window is the unrecoverable "
                        "direction"
                        % (name, win, OBSERVED_FLOOR_HEADROOM, floor))
            continue
        return ("%s pins max_context=%d with no backing — none of %s. A pin "
                "with no recorded evidence is the bare assertion the "
                "no-guessed-window rule exists to stop; omit max_context and "
                "take CC's conservative %d instead"
                % (name, win, ", ".join(WINDOW_BACKINGS),
                   _CC_ASSUMED_WINDOW_MIRROR))
    return ""


def _cwd_moved(prior, seat_name, cwd, flagged):
    """Is placing this seat at `cwd` a MOVE away from the directory its derived
    room was derived from?

    THE FLAG IS A PROXY FOR THE MOVE, NOT THE MOVE ITSELF, and reading it as
    the move is what left `resume` re-deriving on one path only. spawn.json's
    `worktree` is written by the SAME call that writes launch.sh's room, so it
    IS the derivation's input, recorded beside its output: comparing it to
    where the seat is about to land measures the move directly, with no
    opinion about which flag the operator typed. That matters because resume
    relocates seats on its OWN authority — `_resume_cwd` rehomes a pane out of
    the shared checkout or a temp dir, and the CONTEXT_FULL recovery seam
    pins the authoritative worktree — so a BARE resume moves seats routinely
    and a flag-shaped predicate is blind to every one of those moves.

    `flagged` stays in as an OR, not a fallback: an operator naming a cwd is
    asking for that cwd to decide, and a seat with no recorded worktree has no
    measurement to offer. With neither, the answer is "no move" — preserving
    the standing contract that a re-spawn in place changes nothing.
    """
    if flagged:
        return True
    recorded = ((prior.get("room_worktree") or prior.get("worktree"))
                if prior.get("seat") == seat_name else None)
    if not cwd:
        return False
    if not recorded:
        return False
    try:
        return os.path.realpath(recorded) != os.path.realpath(cwd)
    except OSError:
        return False        # unreadable is not evidence of a move


def _room_for_cwd(seat_name, cwd, room, room_source, moved, room_worktree=None):
    """(room, source, derivation-cwd) for a seat being placed at `cwd`.

    `room_worktree` records the input that produced the current derived answer.
    On UNKNOWN it deliberately stays old while the runtime worktree moves, so a
    later bare retry still knows the room is stale and re-derives it.

    THE LAW, in one line each:
      explicit / no opinion  an operator meant it, or nobody has said anything
                             — untouched, whatever the cwd does.
      not a move             a placement in place is not a move; a derived
                             room that has not moved is not stale.
      derived, moved, OK     the room follows the cwd. This is the whole point.
      derived, moved, NONE   the cwd was LOOKED at and has no project: the room
                             is cleared, durably, as `cleared` — the stale
                             carry-forward this exists to stop, on the input
                             where it matters most.
      derived, moved, UNKNOWN  the derivation FAILED. Cannot-look must never
                             render as looked-and-found-nothing: the room and
                             its provenance are left exactly as they were, and
                             the failure is said out loud rather than being
                             spent as a clear. Missing evidence is not
                             evidence against.

    `cleared` re-derives on a later move for the same reason `derived` does —
    it is a derivation's result, not an operator's instruction, so a seat that
    moves from nowhere into a real project picks that project up."""
    from . import seats
    if room_source not in ("derived", seats.ROOM_CLEARED):
        return room, room_source, room_worktree
    if not moved:
        return room, room_source, room_worktree
    status, fresh = seats.derive_home_room_typed(cwd)
    if status == seats.DERIVE_UNKNOWN:
        print("helm seat: WARN — could not derive %s's home room from %s "
              "(the directory or git itself was unreadable). KEEPING the "
              "recorded room %r: a failed derivation is not a measured "
              "absence, and clearing on one would un-home a live seat over a "
              "transient failure. Re-run once the tree is readable, or pass "
              "--room to set it deliberately."
              % (seat_name, cwd, room), file=sys.stderr)
        return room, room_source, room_worktree
    if status == seats.DERIVE_OK:
        return fresh, "derived", cwd
    return None, seats.ROOM_CLEARED, cwd


def _launch_snapshot(path):
    """('present', bytes, mode), ('absent',), or ('unknown',)."""
    try:
        f = open(path, "rb")
    except FileNotFoundError:
        return ("absent",)
    except OSError:
        return ("unknown",)
    try:
        with f:
            data = f.read()
            mode = stat.S_IMODE(os.fstat(f.fileno()).st_mode)
    except OSError:
        return ("unknown",)
    return "present", data, mode


def _restore_launch(path, snapshot, survivor=None):
    """Put a seat's launch script back exactly as it was found — unless a pane
    SURVIVED the failure, in which case it declines and says so.

    A SPAWN THAT FAILS MUST NOT LEAVE THE SEAT CHANGED. The re-mint happens
    BEFORE the pane is created — deliberately, so the pane's `sh` reads
    current code — but that ordering means a spawn which then fails has
    already rewritten the one file every later `launch` and `resume` reads.
    The seat is left carrying a room chosen for a pane that never started:
    measured, a spawn whose adapter refused the pane still moved launch.sh
    from ('proj-x', 'derived') to un-homed, and the next resume of that seat
    took the room of whatever process ran it.

    AND A ROLLBACK IS ONLY A ROLLBACK WHEN THE THING IT UNDOES DID NOT HAPPEN.
    `survivor` is what bounds it, and the bound has to be a MEASUREMENT, not
    the shape of the failure: helm's own cleanup reports UNPROVEN closes
    (`stop_pane`/`terminate_process` return an `unavailable` string exactly
    for this), and every one of those means a pane may still be running the
    script we are about to overwrite. Measured on this seam: an unsubmitted
    onboarding raised, the pane refused to close, helm printed "close
    unproven" — and the rollback restored the previous script anyway, leaving
    a LIVE seat whose environment and home room disagreed with the only file
    that describes it, for every later verb to re-mint from. Restoring over a
    survivor is not undoing a spawn, it is corrupting a live one.

    So: no survivor, the script goes back; a survivor, the new script stays,
    because for that pane the spawn really happened."""
    if snapshot[0] == "unknown":
        return                  # caller refuses before re-minting this state
    if survivor:
        print("helm seat: the failed launch was NOT rolled back — %s. Its "
              "script (%s) is the one that pane is running, so restoring the "
              "previous one would leave a live seat described by a file that "
              "does not match it. Resolve the pane, then re-run the verb."
              % (survivor, path), file=sys.stderr)
        return
    if snapshot[0] == "absent":
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            print("helm seat: WARNING — the failed spawn created %s and it "
                  "could not be removed during rollback: %s" % (path, e),
                  file=sys.stderr)
        return
    _state, data, mode = snapshot
    try:
        with open(path, "rb") as f:
            same = f.read() == data
        if same:
            os.chmod(path, mode)     # re-mint also replaces the file mode
            return
    except OSError:
        pass
    try:
        _write_launch_sh(path, data.decode("utf-8"))
        os.chmod(path, mode)
    except (OSError, UnicodeDecodeError) as e:
        print("helm seat: WARNING — the failed spawn's launch.sh re-mint could "
              "NOT be rolled back (%s): %s. The seat's script now describes a "
              "pane that was never started; re-run the spawn, or `helm seat "
              "launch` to re-mint it deliberately." % (path, e),
              file=sys.stderr)


def _resume(seat_name, rest, _locked=False, target_sid=None,
            expected_session=None, adapter=None, reboot_dead=False,
            reboot_sid=None):
    """seat resume <seat> — relaunch the seat's pane at its drain point via
    the detected metaharness: the pane runs the seat's freshly re-minted
    launch.sh (latest env/identity/hooks) with claude's own continuity flag
    appended (--resume <id> when the seat's config dir names a session, else
    --continue), so the SESSION survives while the environment refreshes.
    TOKEN LAW: the pane command is the launch.sh PATH — the expanded launch
    line (which carries the proxy token) never crosses the adapter seam.

    target_sid/expected_session/adapter are the internal CONTEXT_FULL recovery
    seam: resume the exact cv-pruned copy only while the measured old session
    still owns the authoritative pane. The normal CLI leaves them unset and
    retains newest-session behavior.

    --cwd DIR overrides where the relaunched pane lands (the owner's measured
    workarounds — editing spawn.json's worktree, rehoming the transcript slug
    — do not take, because this path derives cwd from the newest transcript's
    OWN sniffed cwd, not from either). The shared checkout is the sane value
    when the operator wants every pane findable in one tree. NOT COSMETIC:
    cwd decides which BINARY and which TREE the seat acts on — PATH helm
    symlinks to the shared checkout's bin/helm, so a seat in a stale tree
    silently runs different code than it reads (row #119's ungated-approve
    shape). The same reason a recorded cwd that no longer exists, or sits in
    a REMOVED worktree, REFUSES loudly instead of spawning somewhere stale.

    reboot_dead=True is the POST-REBOOT leg (`seat resume --all`): the
    caller's sweep classified this seat DEAD-PANE or PANE-GONE — orca
    replayed `claude --resume <sid>` with no env, the proxy seat's session
    lives under its own CLAUDE_CONFIG_DIR, "No conversation found", and the
    pane is a bare shell or gone. The flag is a REQUEST, never the proof:
    the proof itself (seat_resume_all.prove_reboot_dead — rebind's two
    zero-count refusals, the claude census, the live-session map, the pane
    key's resolution) runs HERE, INSIDE this seat's lifecycle lock, and the
    pane it names is the one written to. A proof taken outside the lock and
    handed in as a handle was a proof-then-act race
    (measured): a hand resume in the gap between the sweep's proof and its
    act was injected over, or its register overwritten. The same in-lock
    proof serves the HAND resume when the reap refuses a register whose
    pane the boot destroyed ("cannot be checked or reaped" — right for the
    reap's own question, fatal for the operator's). On DEAD-PANE nothing is
    reaped and the pane is REUSED (the owner's layout survives): the
    TERMINAL_DISARM line goes first, because a replayed scrollback leaves
    the renderer's mouse-tracking modes armed and the shell printing
    35;5;40M on every mouse move, then the launch line. On PANE-GONE the
    metaharness mints a pane. Anything else aborts rc 1 with the proof's
    reason — a seat that came alive between the sweep's table and its act
    is exactly the case this refusal exists for. reboot_sid=<sid> is the
    sweep's SESSION PIN: the transcript its table classified is the one
    resumed, or nothing is — never "the newest one now", which after a
    transcript vanishes is a DIFFERENT conversation (found on the cut
    that pinned only the zero-transcript case). It pins the session alone:
    the worktree and pane-proof pins of target_sid belong to the
    autocompact seam and are not the sweep's."""
    rest = list(rest)
    into_pane = None
    role_err = _seat_lifecycle_impl._resume_role(rest, {})[1]
    if role_err:
        print("helm seat: " + role_err, file=sys.stderr)
        return 2
    cwd_override = None
    if "--cwd" in rest:
        # guard_tail already proved the value exists and is not a flag
        cwd_override = os.path.abspath(
            os.path.expanduser(rest[rest.index("--cwd") + 1]))
    requested_sid = None
    if "--session" in rest:
        # An OPERATOR's exact pick, for the prune-then-rescue path: cv prints
        # the new id and resume must attach THAT copy, not whichever session
        # ranks newest (the walled original keeps growing until the pane is
        # reaped, so it usually wins a content race against its own prune).
        requested_sid = rest[rest.index("--session") + 1]
    family, err = _seat_family(seat_name)
    if err:
        # An orca-launched claude pane has no launch.sh to re-mint, so its
        # resume is a different act: guard the session, then relaunch from the
        # TRANSCRIPT (the only durable source — closing an orca pane SIGKILLs
        # the agent and orca deletes its own resume record).
        from . import orcaadopt
        if orcaadopt.resolve(seat_name) is None:
            print("helm seat: " + _unknown_seat_reason(seat_name, err),
                  file=sys.stderr)
            return 2
        # reboot_dead is a no-op here: an adopted seat has no register and
        # no pane to reuse, and orcaadopt.resume carries its own liveness
        # refusal, so the sweep's PANE-GONE row and a hand resume are the
        # same call (a review's FIX on the first under-the-lock cut: a
        # refusal here failed every adopted row under --apply, rc 2, with no
        # arm to see it).
        if requested_sid:
            print("helm seat: --session is not supported for an orca-adopted "
                  "seat — its resume derives the session from the transcript "
                  "row (sessions.spawn_resume)", file=sys.stderr)
            return 2
        if cwd_override:
            # honest refusal, never a silent ignore: the adopted path derives
            # its cwd inside sessions.spawn_resume from the transcript row
            print("helm seat: --cwd is not supported for an orca-adopted "
                  "seat yet — its resume derives cwd from the transcript "
                  "row (sessions.spawn_resume)", file=sys.stderr)
            return 2
        if "--role" in rest:
            print("helm seat: --role is not supported for an orca-adopted seat "
                  "— only Helm-managed spawn records can preserve and reassert "
                  "a launch role", file=sys.stderr)
            return 2
        # the hand verb's call is pinned by test_orcaadopt; the sweep's pin
        # rides only when there is one
        rc, lines = orcaadopt.resume(seat_name, force="--force" in rest,
                                     **({"session": reboot_sid}
                                        if reboot_sid else {}))
        for line in lines:
            print(line, file=sys.stderr if rc else sys.stdout)
        return rc
    if family == NATIVE_FAMILY:
        # A REGISTERED NATIVE SEAT RESOLVES HERE NOW (its own register names its
        # family), and that is the point: `where`, the reboot sweep and `up`/
        # `down` all read it through the same resolver. Resume is the one verb
        # whose act it cannot supply — a native seat has NO launch.sh to re-mint
        # and no proxy to restart, and its pane command is `helm launch --seat`,
        # which mints a NEW session rather than continuing one. Say that, named,
        # instead of reaching into FAMILIES for a family that is not in it.
        print("helm seat: %s is a native claude seat — it has no launch.sh to "
              "re-mint and no proxy to restart, so `seat resume` has nothing to "
              "replay; `helm seat spawn %s --replace` relaunches its pane "
              "(`helm seat where %s` reads its register meanwhile)"
              % (seat_name, seat_name, seat_name), file=sys.stderr)
        return 2
    # Same admissibility gate as `_spawn`, BEFORE anything is minted (the
    # MED finding: resume bypassed it, so `resume kimi-2`/`resume codex-1` minted
    # instance assets spawn would have refused).
    gate = _instance_gate(family, seat_name)
    if gate:
        print("helm seat: " + gate, file=sys.stderr)
        return 2
    d = _instance_dir(family, seat_name)
    if not _locked:
        with _seat_lifecycle_lock(d):
            return _resume(seat_name, rest, _locked=True,
                           target_sid=target_sid,
                           expected_session=expected_session,
                           adapter=adapter, reboot_dead=reboot_dead,
                           reboot_sid=reboot_sid)
    launch_sh = os.path.join(d, "launch.sh")
    if not os.path.exists(launch_sh):
        print("helm seat: no %s seat minted (%s missing) — `helm seat add %s` "
              "then `helm seat launch %s` first"
              % (seat_name, launch_sh, family, seat_name), file=sys.stderr)
        return 1
    room, room_source = _homing_from_launch(launch_sh)
    multi = _multi_from_launch(launch_sh)
    prior = _spawn_record(d) or {}
    room_worktree = prior.get("room_worktree") or prior.get("worktree")
    role, role_err = _seat_lifecycle_impl._resume_role(
        rest, prior if prior.get("seat") == seat_name else {})
    if role_err:
        print("helm seat: recorded spawn role is invalid; " + role_err,
              file=sys.stderr)
        return 2
    prior_sid = prior.get("session") if prior.get("seat") == seat_name else None
    # the seat's explicit --model choice survives every env refresh: without
    # this the FIRST resume re-minted launch.sh with no model and rewrote a
    # spark seat (76k window) back to the family default sol+320k — the
    # overstated window compaction cannot recover from (land af391eab).
    prior_model = _persisted_model(d, seat_name)
    from .seat_catalog import proxy_runtime_model_error
    model_error = proxy_runtime_model_error(family, prior_model)
    if model_error:
        print("helm seat: " + model_error, file=sys.stderr)
        return 2
    if requested_sid:
        if not _SESSION_JSONL.match(str(requested_sid) + ".jsonl"):
            print("helm seat: refusing --session %r — not a session id shape; "
                  "cv prune prints one (a bare uuid) when it mints the copy"
                  % requested_sid, file=sys.stderr)
            return 2
        if not _seat_session_path_by_id(d, requested_sid):
            recent = sorted(
                (p for p in glob.glob(os.path.join(
                    d, "claude", "projects", "*", "*.jsonl"))
                 if _SESSION_JSONL.match(os.path.basename(p))),
                key=lambda p: -os.stat(p).st_mtime)[:3]
            print("helm seat: refusing --session %s — no such transcript in "
                  "this seat's config home. Recent sessions here: %s"
                  % (str(requested_sid)[:12], ", ".join(
                      os.path.basename(p)[:8] for p in recent) or "none"),
                  file=sys.stderr)
            return 2
        target_sid = requested_sid
    if expected_session and prior_sid != expected_session:
        print("helm seat: refusing to resume %s — registered session changed "
              "from expected %s to %s" % (
                  seat_name, str(expected_session)[:12], str(prior_sid)[:12]),
              file=sys.stderr)
        return 1
    sid, sess_cwd = (_seat_session_by_id(d, target_sid) if target_sid
                     else _seat_session_by_id(d, reboot_sid) if reboot_sid
                     else _newest_seat_session(d, prefer_source=prior_sid))
    if reboot_sid and not sid:
        # NO NEW SESSION IS EVER MINTED by a reboot relaunch: the pinned
        # session is resumed or nothing is — never `--continue` (a fresh
        # conversation called a resume, the outcome the owner named a
        # failure) and never the newest OTHER transcript.
        print("helm seat: refusing to resume %s — the session the sweep "
              "classified, %s, is no longer one real transcript in this "
              "seat's config home; a reboot relaunch resumes THAT session or "
              "none (never the newest other one)"
              % (seat_name, str(reboot_sid)[:12]), file=sys.stderr)
        return 1
    if target_sid and not sid:
        print("helm seat: refusing to resume %s — exact session %s is not one "
              "real transcript in this seat's config home" %
              (seat_name, str(target_sid)[:12]), file=sys.stderr)
        return 1
    if target_sid and not requested_sid:
        # The autocompact CONTEXT_FULL seam additionally pins the authoritative
        # worktree; an operator's --session keeps the ordinary cwd resolution.
        prior_worktree = (prior.get("worktree")
                          if prior.get("seat") == seat_name else None)
        if not isinstance(prior_worktree, str) or not prior_worktree \
                or not os.path.isdir(prior_worktree):
            print("helm seat: refusing exact recovery for %s — authoritative "
                  "worktree is missing or unavailable: %r" %
                  (seat_name, prior_worktree), file=sys.stderr)
            return 1
        resume_cwd = prior_worktree
    else:
        resume_cwd = cwd_override or _resume_cwd(seat_name, sess_cwd)
    stale_why = _stale_resume_cwd(resume_cwd)
    if stale_why:
        # REFUSE BEFORE REAPING: the gate must fire while the seat still has
        # its old pane — a refusal after _reap_stale would leave no pane at
        # all, which is worse than the stale spawn it prevents.
        from . import harness as _harness, seats as _seats
        shared = _harness.find_repo_root(_seats.safe_cwd()) or _seats.safe_cwd()
        print("helm seat: REFUSING to resume %s at %s — %s. A pane spawned "
              "at a stale cwd acts on a different tree than the code it runs "
              "(PATH helm resolves through the shared checkout), which is how "
              "a stale seat checkout writes ungated approves. Rerun with an "
              "explicit working directory — the shared checkout is the sane "
              "default: helm seat resume %s --cwd %s"
              % (seat_name, resume_cwd, stale_why, seat_name,
                 shlex.quote(shared)), file=sys.stderr)
        return 1
    from . import seats
    if room is None and room_source != seats.ROOM_CLEARED:
        # launch.sh carries no room stamp AND no opinion (minted room-less).
        # Resume must still preserve the seat's DERIVABLE home — re-minting
        # with room=None would stamp the relaunch HELM_CHAT_ROOM-less and the
        # SessionStart join would fall back to #main, silently dropping the
        # seat out of its project room (the kimi room-drop regression,
        # 2026-07-23).
        #
        # DERIVE FROM THE RESUME CWD ALONE. This used to call resolve_homing,
        # whose SECOND rung is the env seam — and the env here belongs to the
        # process running `helm seat resume`, which is nearly always another
        # seat. So the invoker's own HELM_CHAT_ROOM outranked the directory
        # the pane was about to land in, and — having no _SOURCE stamp — it
        # was recorded as EXPLICIT, a tier that then outranks every later
        # derived join. Measured: `resume <seat> --cwd <project>` with an
        # ambient room exported wrote that ambient room into launch.sh,
        # spawn.json AND the roster, and the target project lost. A seat's
        # home is a fact about the seat, never about who typed the verb.
        status, derived = seats.derive_home_room_typed(resume_cwd)
        if status == seats.DERIVE_OK:
            room, room_source, room_worktree = derived, "derived", resume_cwd
    else:
        # THE SAME DEFECT ON THE SECOND PATH, and the flag was never the
        # discriminator — see _cwd_moved. A resume relocates seats on its own
        # authority (rehoming out of the shared checkout or a temp dir, and
        # the CONTEXT_FULL seam's authoritative worktree), so a BARE resume
        # moves them routinely and re-derives none of it while `--cwd` alone
        # did. One helper, one law, both paths.
        room, room_source, room_worktree = _room_for_cwd(
            seat_name, resume_cwd, room, room_source,
            _cwd_moved(prior, seat_name, resume_cwd, bool(cwd_override)),
            room_worktree)
    # Resolve identity BEFORE the reap below. A renamed roster that is
    # unavailable or ambiguous must refuse while the old pane and launch.sh are
    # still untouched — discovering that
    # only inside _write_launch_assets would turn an identity refusal into an
    # outage.
    from .seat_launch_assets import _launch_identity
    identity, identity_error = _launch_identity(seat_name)
    if identity_error:
        print("helm seat: REFUSED — launch identity for %s is unresolved: %s; "
              "the existing pane and launch asset are unchanged"
              % (seat_name, identity_error), file=sys.stderr)
        return 1
    # THE IN-FLIGHT DOOR, BEFORE THE FIRST WRITE OF ANY KIND. A spawn that has
    # published its PENDING attempt but not yet finished owns this seat while its
    # lifecycle lock is released, so the flock cannot say so — and this verb's
    # first write is not the reap below: the no-adapter branch RE-MINTS the
    # launch assets (`_write_launch_assets`) and returns a manual command, and
    # the first cut asked this question only after that branch had returned. A
    # standalone `resume S --cwd <valid other cwd>` during a proxy spawn's child
    # window therefore rewrote the room, launch line and settings of the seat the
    # spawn was still bringing up, and the spawn then finalized over them. The
    # guard moves ahead of the write; the write does not move behind the guard.
    if _refuse_in_flight_spawn(d):
        return 1
    # ALLOCATE THE ENDPOINT NOW — the same point in the same order `_spawn`
    # reaches it: past EVERY deterministic refusal (the launch-asset check, the
    # persisted role, the runtime model, the requested/expected session, the
    # resolved transcript, the stale cwd, the launch identity, the in-flight
    # attempt) and immediately before the reap and the re-mint, which are the
    # acts that write this seat's port into its launch line and its proxy config.
    #
    # RESUME MINTS TOO — the MED finding that moved the admissibility gate onto
    # this verb in the first place — so it owns the same commit. What it did NOT
    # own was the ordering: the commit sat above the role, model and session
    # gates, so a registered project seat carrying a malformed persisted role
    # returned 2 without minting or launching anything and kept a durable slot
    # out of a 100-wide span that nothing ever reclaims. The whole reason the
    # allocation is split from the admission predicate is that a refusal must
    # spend nothing, and this verb was spending on its own refusals.
    endpoint_err = _ensure_instance_endpoint(family, seat_name)
    if endpoint_err:
        print("helm seat: %s cannot be resumed — %s"
              % (seat_name, endpoint_err), file=sys.stderr)
        return 1
    command = _seat_lifecycle_impl._launch_command(
        launch_sh, role,
        tail=("--resume", sid) if sid else ("--continue",))
    from . import harness
    ad = adapter or harness.detect()
    if ad is None:
        # A manual paste still executes launch.sh, so refresh that asset before
        # returning its command. The old path printed "env refreshed" while
        # leaving a stale pre-rename script untouched.
        launch_was = _launch_snapshot(launch_sh)
        if launch_was[0] == "unknown":
            print("helm seat: refusing to re-mint unreadable launch script %s; "
                  "rollback could not restore bytes it cannot capture" % launch_sh,
                  file=sys.stderr)
            return 1
        try:
            refused = _write_launch_assets(
                family, d, room, seat_name,
                room_source=room_source, multi=multi,
                model=prior_model, identity=identity) is _SEAT_SURFACE_REFUSED
        except OSError as e:
            _restore_launch(launch_sh, launch_was)
            print("helm seat: launch asset re-mint failed: %s" % e,
                  file=sys.stderr)
            return 1
        if refused:
            _restore_launch(launch_sh, launch_was)
            return 1
        print("helm seat: " + harness.RECOMMENDATION, file=sys.stderr)
        print("  manual paste (env refreshed, session kept): " + command,
              file=sys.stderr)
        return 1
    # Stop only the authoritative recorded process/pane BEFORE re-minting: its
    # `sh` is executing THIS launch.sh. Mutable titles are never identity; an
    # unregistered same-title pane blocks the resume instead of being destroyed.
    # The reboot-dead leg skips the reap (the register's handle names a pane
    # the boot destroyed, which the reap resolver would — correctly, for its
    # own question — refuse) and PROVES DEATH HERE, UNDER THE LOCK. The hand
    # resume reaches the same proof when the reap refuses: that refusal is
    # the one state an operator typing `seat resume <seat>` the morning
    # after is here FOR.
    errors = []
    if not reboot_dead:
        notes, errors = _reap_stale(
            seat_name, d, ad, allow_live=True, locked=True,
            defer_orca_terminal=True)
        for note in notes:
            print("  " + note)
    prove = reboot_dead or bool(errors)
    terminal_proof = None

    def proven_dead(final):
        """The death proof, under the lock, taken TWICE on purpose. EARLY
        (final=False), right here before any side effect, so a seat that is
        not a proven casualty refuses with nothing re-minted — the resume
        arms pin that a refused resume touches no launch asset. LATE
        (final=True), after the launch assets and the proxy are minted and
        immediately before the first byte reaches the pane: the lock
        serializes helm's seat operations, not the pane, so the window
        between the binding proof and the send is helm's own work and
        nothing else (a review's residual on the first under-the-lock cut).
        The pane written to is the LATE proof's. The sweep's request stays
        BOOT-BOUND in both: a register a hand resume wrote after the boot,
        whose pane then died, is a post-boot casualty for an operator,
        never the timer's to relaunch."""
        nonlocal terminal_proof
        from . import seat_resume_all
        state, pane, why = seat_resume_all.prove_reboot_dead(
            seat_name, ad, bind_boot=reboot_dead)
        if state not in (seat_resume_all.DEAD_PANE, seat_resume_all.PANE_GONE):
            for error in errors:
                print("helm seat: " + error, file=sys.stderr)
            print("helm seat: reboot-dead proof under the seat lock%s: %s — %s"
                  % (" (at the send)" if final else "", state, why),
                  file=sys.stderr)
            print("helm seat: resume aborted; the seat is not a proven reboot "
                  "casualty%s" % ("; resolve the unverified same-name pane "
                                   "before retrying" if errors else ""),
                  file=sys.stderr)
            return False, None
        if final:
            terminal_proof = "%s under the seat lock — %s" % (state, why)
            print("  %sseat proven %s"
                  % ("reap refused (%s); " % "; ".join(errors) if errors
                     else "", terminal_proof))
        return True, pane

    if prove and not proven_dead(False)[0]:
        return 1
    # A RESUME THAT FAILS MUST NOT LEAVE THE SEAT CHANGED either — the re-mint
    # below rewrites the one script every later launch reads, and every return
    # between here and a REGISTERED pane leaves the seat with no pane at all.
    # BOUNDED BY PANE SURVIVAL: `survivor` is passed wherever the cleanup could
    # not PROVE the pane gone, because restoring over a running pane is
    # corruption, not a rollback (see _restore_launch).
    launch_was = _launch_snapshot(launch_sh)
    if launch_was[0] == "unknown":
        print("helm seat: refusing to re-mint unreadable launch script %s; "
              "rollback could not restore bytes it cannot capture" % launch_sh,
              file=sys.stderr)
        return 1

    def _unwound(survivor=None):
        _restore_launch(launch_sh, launch_was, survivor)
        return 1
    in_place_launch_attempted = False
    spawn_attempted = False
    try:
        # env refresh half of the contract: the relaunch rides the LATEST
        # assets (identity vars, delivery hooks, context env), room preserved.
        if _write_launch_assets(
                family, d, room, seat_name,
                room_source=room_source, multi=multi,
                model=prior_model, identity=identity) \
                is _SEAT_SURFACE_REFUSED:
            return _unwound()
        from . import seats
        # resume must not strand the seat on a dead proxy either (the same
        # silent-dead-seat class the HIGH finding named in _spawn): mint the
        # instance proxy (idempotent) and start it if down, so the relaunched
        # seat's 8319 line has a live proxy behind it.
        fam = FAMILIES[family]
        if seat_name != family and fam["mode"] == "proxy":
            _mint_instance_proxy(family, seat_name)
        if os.path.exists(os.path.join(_proxy_home(family, seat_name),
                                       "config.yaml")) \
                and not _running_pid(family, seat_name):
            if _up(family, quiet=True, seat=seat_name) == 0:
                print("  (proxy was down — auto-started)")
        # Resolved ONCE before any reap and reused for spawn + register below:
        # exact recovery uses spawn.json's authoritative worktree, never a
        # transcript sniff or the timer process's cwd.
        if prove:
            ok, into_pane = proven_dead(True)
            if not ok:
                return _unwound()
            from . import seat_exit_owner
            current = _spawn_record(d)
            archive_notes, archive_errors = seat_exit_owner.close_spawn(
                seat_name, d, current, terminal_proof)
            for note in archive_notes:
                print("  " + note)
            if archive_errors:
                for error in archive_errors:
                    print("helm seat: proven terminal runtime was not archived: "
                          + error, file=sys.stderr)
                return _unwound()
        if into_pane:
            # DISARM FIRST, THEN LAUNCH, as two sends in that order: the
            # disarm is a shell line that writes TERMINAL_DISARM to the pane's
            # own terminal (the bytes must reach the RENDERER, which only a
            # process writing to the pty can do — sent as keystrokes they
            # would be readline input), and the launch line rides the same
            # adapter seam into the same pane.
            from . import seat_resume_all
            ad.send(into_pane, seat_resume_all.disarm_line(), enter=True)
            in_place_launch_attempted = True
            ad.send(into_pane, seat_resume_all.pane_launch_line(
                command, resume_cwd or seats.safe_cwd()), enter=True)
            handle = into_pane
        else:
            spawn_attempted = True
            handle = ad.spawn(command, title=identity,   # safe_cwd: a deleted
                              cwd=resume_cwd or seats.safe_cwd())  # cwd must
        # not crash the resume (eager-getcwd class); spawn treats None as inherit.
    except (harness.HarnessError, OSError) as e:
        print("helm seat: %s resume via %s failed: %s"
              % (seat_name, ad.name, e), file=sys.stderr)
        # `ad.spawn` raising leaves no pane at all, so the script goes back.
        # The in-place leg is the other pole: that pane EXISTS, helm never
        # attempted to close it, and the failure may be the adapter reporting
        # on a launch line that already reached the pty — an already-minted
        # relaunch reading the script we would be overwriting. Unmeasured is
        # not proven-gone, and the rollback is bounded by the proof.
        proved_absent = not spawn_attempted or getattr(
            ad, "spawn_failure_absent", lambda _e: False)(e)
        survivor = (("pane %s was relaunched in place and was not closed; "
                     "whether the launch line reached it is unmeasured"
                     % into_pane) if into_pane and in_place_launch_attempted
                    else None if into_pane or proved_absent else
                    "the adapter may have created a remote pane before its "
                    "create reply failed; no handle exists to prove it absent")
        return _unwound(survivor)
    def stop_new():
        """Close the pane this resume created and RETURN whether it survived —
        the string names the survivor for `_unwound`, None means proven gone.
        The return value is the whole point: the callers below all restore the
        launch script, and that is only legal when this proved the pane dead."""
        try:
            from . import seat_exit_owner
            _reason, unavailable = seat_exit_owner.stop_pane(ad, handle)
            if unavailable:
                print("helm seat: WARNING — unregistered resumed pane %s close "
                      "was not proven: %s" % (handle, unavailable),
                      file=sys.stderr)
                return "resumed pane %s could not be proven closed (%s)" \
                    % (handle, unavailable)
        except Exception as e:
            print("helm seat: WARNING — unregistered resumed pane %s could not "
                  "be closed: %s" % (handle, e), file=sys.stderr)
            return "resumed pane %s could not be closed (%s)" % (handle, e)
        return None

    if target_sid:
        live, live_err = _prove_spawned_pane(ad, handle)
        if not live:
            print("helm seat: resumed pane was not proven live: %s" % live_err,
                  file=sys.stderr)
            return _unwound(stop_new())

    moved = []
    if prior_sid and sid and prior_sid != sid:
        try:
            moved = seats.rebind_claim_sessions(identity, prior_sid, sid)
        except Exception as e:
            print("helm seat: claim-session rebind failed before registration: %s"
                  % e, file=sys.stderr)
            return _unwound(stop_new())

    from . import pk
    # FAMILY AND PROJECT RIDE EVERY REGISTER THIS LEG WRITES, exactly as the
    # spawn door writes them. A project seat's family is not in its name: it is
    # in this record (`registered_seat_family`), so a register without it
    # resolves to nothing — every later `_seat_family` answers "unknown seat",
    # the reboot sweep's first rung returns UNKNOWN and never re-stamps it, and
    # the seat's next resume cannot find its own register. `family` is the one
    # this resume already resolved and acted on; `project` is carried from the
    # record being replaced, identity-guarded the way `role` and the session
    # are, because a resume never re-decides which project a seat serves.
    rec = {"v": 1, "seat": seat_name, "identity": identity, "role": role,
           "family": family,
           "project": prior.get("project")
           if prior.get("seat") == seat_name else None,
           "worktree": resume_cwd or os.getcwd(),
           # NEVER a defaulted 'main'. `room or "main"` turned both an
           # un-homed seat AND a deliberately cleared one into a seat homed in
           # #main -- inventing a home the SessionStart join would never have
           # written, which is the roster-scatter class _spawn's own record
           # already refuses (test_room_less_spawn_mirror_never_invents_a_main
           # _home states the law; only this leg was breaking it). Readers of
           # this field already spell `rec.get("room") or "main"` themselves.
           "room": room, "room_source": room_source,
           "room_worktree": room_worktree,
           "launch_sh": launch_sh, "model": prior_model, "ts": pk.now_ts(),
           "harness": ad.name, "handle": handle, "session": sid}
    if not _register_spawn(seat_name, identity, d, rec):
        try:
            rolled = seats.rollback_claim_sessions(
                identity, prior_sid, sid, moved)
            if rolled != len(moved):
                print("helm seat: WARNING — claim rollback restored %d/%d rows"
                      % (rolled, len(moved)), file=sys.stderr)
        except Exception as e:
            print("helm seat: WARNING — claim rollback failed: %s" % e,
                  file=sys.stderr)
        return _unwound(stop_new())
    if moved:
        print("  rebound %d live claim lease%s from session %s… to %s…" %
              (len(moved), "" if len(moved) == 1 else "s",
               prior_sid[:8], sid[:8]))
    if isinstance(ad, harness.OrcaAdapter) and not \
            _backfill_spawn_session(seat_name, d, ad):
        print("helm seat: WARN — resumed pane session identity is not yet "
              "proven; SessionStart must bind it before autocompact can act",
              file=sys.stderr)
    _ensure_autocompact_timer()
    # THE WAKE-PATH LEG (row #153): the relaunched pane is alive but DEAF —
    # its beacon was a per-session Monitor the restart killed, and a resume is
    # not a new session, so nothing gives it the turn the SessionStart arm
    # directive needs. Inject that turn (spawn's own delivery shape: boot
    # grace, then the prompt as keystrokes). A restart that cannot restore the
    # wake path REFUSES TO REPORT ITSELF COMPLETE — rc 1, said loudly — never
    # "resumed" over a seat nothing can reach (2026-08-03: six of seven seats
    # restored deaf, the fleet unreachable for hours and reading as idle).
    try:
        delay = float(home.env("SPAWN_SEND_DELAY", SPAWN_SEND_DELAY_S))
    except (TypeError, ValueError):
        delay = SPAWN_SEND_DELAY_S
    if delay > 0:            # let claude re-reach its composer before the
        time.sleep(delay)    # re-arm keystrokes land
    # SUBMIT, not send: this leg's whole point is that a seat which cannot be
    # woken must not report itself resumed, and "the metaharness accepted the
    # bytes" never proved the re-arm prompt got a TURN. A directive typed into
    # the composer and left unsent produces exactly the alive-and-deaf seat
    # this message describes, while `send` returns success. The exact-visible
    # bootstrap expands the long role-aware brief inside the resumed process;
    # weakening exact composer identity for a wrapped long prompt would let a
    # human suffix earn Enter. The re-arm also reasserts the persisted role.
    #
    # AND IT OBEYS THE SAME PAUSE EVERY OTHER DELIVERY OBEYS. A seat whose
    # proxy pool is cooling down refuses the turn this keystroke would start,
    # prints one more refusal, and is no more re-armed than before; the pane
    # is up, the re-arm is HELD with the reset instant, and resume-turn's
    # nudge after the reset is the wake that goes through.
    from . import poolwall
    held = poolwall.rearm_hold(seat_name, sid)
    if held:
        _record_onboarding(d, seat_name, "HELD", held)
        print("helm seat: resume of %s relaunched pane %s but its wake-path "
              "re-arm is HELD — %s. The seat is ALIVE AND DEAF until then; "
              "after the reset, `helm seat resume %s` or resume-turn's nudge "
              "re-arms it." % (identity, handle, held, identity),
              file=sys.stderr)
        return 1
    state, proof = ad.submit(handle, _boot_brief_wire(rearm=True))
    # The resume leg registers BEFORE it submits, so unlike spawn it
    # cannot carry this in the record it wrote; it needs its own write.
    _record_onboarding(d, seat_name, state, proof)
    if state != harness.DELIVERED:
        from . import seats_advice
        print("helm seat: resume of %s INCOMPLETE — pane %s is up but its "
              "wake path is NOT PROVEN re-armed (%s: %s). The seat may be "
              "ALIVE AND DEAF: no @mention, DM or brief can wake it until its "
              "beacon is re-armed. By hand, type into the pane: arm %s"
              % (identity, handle, state, proof,
                 seats_advice.beacon_monitor(identity)), file=sys.stderr)
        return 1
    print("helm seat: resumed %s via %s — pane %s, %s; env refreshed from %s; "
          "wake-path re-arm prompt sent (the beacon is per-session — the "
          "restart killed it)"
          % (identity, ad.name, handle,
             ("session %s… (--resume)" % sid[:8]) if sid
             else "--continue (newest session)", launch_sh))
    return 0


def _project_tla_notice(seat_name, family, project):
    """One advisory line when this project already has a seat of this family,
    else None (task/2440; whether it REFUSES is task/2449's call).

    A project's second same-family seat is exactly the world the
    project-canonical name exists to end — a numbered seat quietly serving one
    project while every surface named a fleet number — so the operator is TOLD
    which seat already holds that role rather than finding out from a premise
    weeks later. A partly readable seat tree says so: a short list here would
    read as "no other seat" and that is the one reading it must not give.
    """
    if not project:
        return None
    names, blind = registered_seats()
    holders = []
    for name in sorted(names):
        if name == seat_name:
            continue
        tail = name.rpartition("-")[2]
        fam, _err = _seat_family(name)
        fam = fam or (tail if tail in project_families() else name)
        rec = _spawn_record(_instance_dir(fam, name)) or {}
        held = rec.get("project")
        if held is None and tail in project_families():
            held = name.rpartition("-")[0] or None
        if held == project and fam == family:
            holders.append(name)
    if not holders:
        return None
    return ("NOTICE — project %s already has a %s seat: %s. The canon is one "
            "claude seat and one codex seat per project, so one of these is "
            "surplus; this spawn is not refused%s"
            % (project, family, ", ".join(holders),
               " (the seat tree was only partly readable, so that list may be "
               "short)" if blind else ""))


def _project_scope_prefixes():
    """({project: [paths]}, None) or (None, why) — every path the REGISTRY says
    belongs to each project.

    Three tiers, all of them the registry's own and none of them a guess: the
    canonical checkout `path`, every observed `cwds` entry (a linked worktree
    lives in a SIBLING directory and shares no prefix with the root), and
    `cv_scope.cwd_prefixes`, which is the set recall already prefix-matches on.
    Strict, because an unreadable authority root that read as "no projects"
    would refuse every workspace there is and blame the operator.
    """
    from . import registry
    try:
        reg = registry.load(strict=True)
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e)
    scopes = {}
    for name, rec in (reg.get("projects") or {}).items():
        paths = set()
        if rec.get("path"):
            paths.add(rec["path"])
        paths.update(rec.get("cwds") or ())
        paths.update((rec.get("cv_scope") or {}).get("cwd_prefixes") or ())
        # CANONICAL ON BOTH SIDES. A registered path can legitimately contain a
        # symlink (a deployment that links /home, a checkout reached through a
        # convenience link), and a scope written in one spelling can only be
        # compared to a workspace resolved in another if both are resolved. The
        # comparison itself is `_workspace_project`, and it is given canonical
        # targets by both of its callers or it is a lexical check wearing a
        # canonical name.
        scopes[name] = sorted(
            os.path.realpath(os.path.expanduser(path)) for path in paths if path)
    return scopes, None


def _workspace_project(path, scopes):
    """Which registered project OWNS `path` — longest prefix first, or None.

    SCOPE, NOT CONTAINMENT, and the two differ in both directions: a project's
    `-wt` lane worktrees are NOT under its root yet are certainly its own (which
    is why every recorded cwd and cv prefix counts), while `/dev/ab` merely
    BEGINS with the name of a project rooted at `/dev/a` and is not its at all
    (which is why the match is per-path-segment and never a bare startswith).
    """
    here = os.path.realpath(os.path.expanduser(path or ""))
    best, best_len = None, -1
    for name, prefixes in scopes.items():
        for prefix in prefixes:
            if (here == prefix or here.startswith(prefix + os.sep)) \
                    and len(prefix) > best_len:
                best, best_len = name, len(prefix)
    return best


def _main_checkout(root):
    """The MAIN checkout of a repository, given any of its worktrees, or None.
    `git rev-parse --git-common-dir` names the shared `.git`, so its parent is
    the main checkout — and for the main checkout itself it is `root` again. A
    registry entry usually records that path, so a spawn run from inside a
    registered project's LANE worktree must be attributed through it."""
    from .work._lanes import _git
    rc, out, _err = _git(root, "rev-parse", "--path-format=absolute",
                         "--git-common-dir")
    lines = (out or "").strip().splitlines()
    if rc != 0 or not lines:
        return None
    # realpath, not abspath: git answers with the path it was asked through, so a
    # common dir reached via a symlink comes back in the caller's spelling and
    # would not compare equal to the canonical scope it belongs to.
    return os.path.dirname(os.path.realpath(lines[0].strip()))


def _nearest_existing_dir(ref):
    """The nearest EXISTING ancestor of `ref`, CANONICAL, or ''.

    A seat's default home does not exist yet by design
    (`_seat_home_cwd(provision=False)` resolves a prospective path), so the
    question has to be asked of the nearest place that does exist — and then of
    the directory that place REALLY IS.

    THE LEXICAL ANSWER IS NOT AN ANSWER, and one symlink is the whole defect:
    with `A/link -> U`, `abspath("A/link")` is a path under registered project A
    while every byte written through it lands in unregistered U. The scope check
    admitted it, `ad.spawn` entered U, and the register claimed A. Resolving
    HERE, before the comparison, is what makes the attribution about the
    directory that receives the writes. Walk up lexically (the nonexistent tail
    has nothing to resolve), then canonicalize the part that exists.

    Still pure: realpath is a readlink walk, not git and not a subprocess.
    """
    here = os.path.abspath(os.path.expanduser(ref or ""))
    while here and not os.path.isdir(here) and here != os.path.dirname(here):
        here = os.path.dirname(here)
    return os.path.realpath(here) if here and os.path.isdir(here) else ""


def _workspace_repo_candidates(ref):
    """[paths] the REPOSITORY tier of an attribution — the root above `ref`, and
    when that root is a LINKED WORKTREE the main checkout it belongs to, which is
    the path a registry entry usually names.

    SEPARATE FROM THE PURE TIER BECAUSE IT COSTS GIT SUBPROCESSES. A `--print`
    plan promises to spawn, reap and re-mint nothing, and an ordinary spawn's
    home sits inside its project's own scope where a string comparison is the
    whole answer — so this tier is asked only when that one has already failed.
    """
    here = _nearest_existing_dir(ref)
    if not here:
        return []
    from . import harness
    root = harness.find_repo_root(here)
    if not root:
        return []
    out = [os.path.realpath(root)]
    main = _main_checkout(root)
    if main and main not in out:
        out.append(main)
    return out


def _workspace_candidates(ref):
    """Both tiers in order, for a caller that wants the whole attribution set."""
    here = _nearest_existing_dir(ref)
    out = [here] if here else []
    return out + [c for c in _workspace_repo_candidates(ref) if c not in out]


def _spawn_workspace_ref(rest, cwd):
    """THE SECOND path whose ownership decides this spawn's workspace.

    A default home is CARVED FROM the repository the operator is standing in —
    `_seat_home_cwd`'s own reference, `seats.safe_cwd()` — so that repository is
    half the question and the resolved home is the other half. Asking only the
    resolved home would refuse a legitimate FIRST spawn, whose `<repo>-wt/seats/
    <seat>` has no existing ancestor inside the project yet; asking only the
    reference would bind the check to a RE-DERIVATION of the value instead of
    the value the act uses, which is how a check ends up true about a different
    world than the one being written. An explicit `--cwd` is its own reference,
    so the two collapse to one path and nothing is loosened.
    """
    if "--cwd" in rest:
        return cwd
    from . import seats
    return seats.safe_cwd()


def _spawn_scope_error(seat_name, project, cwd, ref):
    """None, or why this spawn's workspace does not belong to the named project.

    THE REGISTRY IS THE AUTHORITY FOR BOTH HALVES OF A PROJECT-CANONICAL NAME.
    Proving the first half alone admits this failure: `acme-codex` shows that
    `acme` is registered, and the spawn then provisions a home worktree and
    cuts a seat BRANCH in whatever repository the operator happens to be
    standing in. The registry holds A, the operator stands in an unregistered
    checkout U, `helm seat spawn A-codex` writes U-wt/seats/A-codex plus a
    branch in U, and records project=A on the seat — after which every reader
    believes A about bytes that live in U.

    RUNS BEFORE ANY PROVISIONING. A refusal that arrives after
    `git worktree add` has already created the thing it refuses.
    """
    scopes, blind = _project_scope_prefixes()
    if scopes is None:
        return ("seat %s names project %s and the registry cannot be read (%s) "
                "— helm will not provision a workspace it cannot attribute"
                % (seat_name, project, blind))
    asked = []

    def ask(path):
        if not path or path in asked:
            return False
        asked.append(path)
        return _workspace_project(path, scopes) == project

    # TIER 1 IS PURE, and it is the tier the ordinary case is answered by: a
    # seat's home sits inside its own project's scope, so a prefix comparison
    # settles it. Asking git first would make a `--print` plan — whose whole
    # contract is that it does nothing — pay for subprocesses to learn what a
    # string already said.
    for path in (cwd, ref):
        if ask(_nearest_existing_dir(path)):
            return None
    # TIER 2 asks git, and only now: the repository above the path and, when that
    # is a linked worktree, the MAIN checkout the registry records. This is the
    # tier a spawn run from inside a registered project's LANE worktree needs.
    for path in (cwd, ref):
        for candidate in _workspace_repo_candidates(path):
            if ask(candidate):
                return None
    held = next((o for o in (_workspace_project(c, scopes) for c in asked)
                 if o), None)
    return ("seat %s is project %s's seat and its workspace is not %s's — it is "
            "%s. helm asked about %s. A registered project's own checkout, any "
            "registered linked worktree of it, or anything under its recorded "
            "cv scope is admitted; nothing else is, because the seat name is "
            "what every later reader trusts about where its work lives. %s's "
            "registered scope is %s; pass --cwd inside it, or `helm sync` to "
            "register this checkout."
            % (seat_name, project, project,
               ("project %s's" % held) if held
               else "not registered to any project",
               ", ".join(asked) or "(no resolvable directory)",
               project, ", ".join(scopes.get(project) or ()) or "(none)"))


def _spawn_reap(seat_name, d, ad, replace):
    """THE SHARED duplicate-and-stale rung: 0 to proceed, 1 to refuse.

    Both adapters call this, and the reason is the whole of finding 2. A LIVE
    registered seat of this exact name is a duplicate, and `_reap_stale` is
    where helm already knows that — it refuses without `--replace`, it reaps
    with one, and it refuses rather than guess when a runtime's terminal state
    is unproven. The native leg had none of it: two `spawn <project>-claude`
    calls left two live panes and the second register silently replaced the
    first, so the seat helm could still name was the one nobody was watching.

    THE ABORT AND THE LAUNCH ARE ONE DECISION. Every caller returns on a 1 from
    here, and the refusal now SAYS so: an operator who reads "replacement
    aborted" and then watches a pane appear cannot tell which half of the
    sentence to believe, and will go looking for the seat under the register
    the refusal did not write. The no-launch and no-re-point facts are stated
    beside the evidence rather than left to be inferred from a return code the
    operator never sees, and the register path is named because it is WHERE the
    stale evidence lives — a refusal that describes a ghost without saying which
    file holds it leaves the operator grepping. The clearing verbs are named for
    the same reason: "resolve the stale same-name seat" is an instruction with
    no door in it.

    "WAS NOT RE-POINTED" IS A CLAIM ABOUT THE DISK, and `_reap_stale` is what
    keeps it true: it decides from read-only evidence and writes the register
    and its archive only after the runtime it names is positively closed, so
    every error it returns describes a seat whose files are byte-identical.
    """
    notes, reap_errors = _reap_stale(
        seat_name, d, ad, allow_live=replace, locked=True)
    for note in notes:
        print("  " + note)
    if not reap_errors:
        return 0
    for error in reap_errors:
        print("helm seat: " + error, file=sys.stderr)
    # The old sentence is KEPT WORD FOR WORD inside the new one. Three shipped
    # arms assert its ABSENCE to prove a different door refused (the in-flight
    # attempt one), and rewording it would have turned all three vacuous while
    # they stayed green — a rename that silently disarms a negative assertion
    # is the same defect class as deleting it.
    print("helm seat: replacement aborted — NOTHING was launched and %s's "
          "register was not re-pointed; resolve the stale same-name seat "
          "before retrying. The stale evidence is above; it lives in %s. "
          "Clear it with `helm seat rebind %s --apply` (re-point the register "
          "at the pane the seat's live process occupies now) or read what the "
          "register still claims with `helm seat where %s`."
          % (seat_name, _spawn_path(d), seat_name, seat_name),
          file=sys.stderr)
    return 1


def _spawn_submit(ad, handle, seat_name):
    """THE SHARED onboarding submit -> (state, proof); raises HarnessError on a
    PROVEN non-delivery.

    A name in the roster arms no beacon and takes no work: the seat has to take
    a TURN, and submitting the short exact-visible bootstrap is how a spawn
    delivers one. A leg that spawns a pane running `helm launch` and stops there
    leaves a bare composer and returns 0 — self-onboarding promised by the
    synopsis and by nothing that runs. SessionStart instructions are context,
    not a submitted turn. The tri-state is preserved rather than folded into success,
    here for every family at once: DELIVERED earns the success sentence,
    NOT_DELIVERED is proven un-briefed and raises so the seconds-old pane can be
    closed, and UNKNOWN is neither — destroying what we could not measure is
    worse, so it is reported loudly and left standing.
    """
    from . import harness
    try:
        delay = float(home.env("SPAWN_SEND_DELAY", SPAWN_SEND_DELAY_S))
    except (TypeError, ValueError):
        delay = SPAWN_SEND_DELAY_S
    if delay > 0:            # let claude reach its composer before the
        time.sleep(delay)    # onboarding keystrokes land
    state, proof = ad.submit(handle, _boot_brief_wire())
    if state == harness.NOT_DELIVERED:
        raise harness.HarnessError("onboarding was not submitted — %s" % proof)
    return state, proof


def _native_launch_argv(seat_name, room, model=None):
    """The native pane's command argv. THE STABLE ENTRYPOINT, never this
    module's own checkout: a seat outlives the lane room a spawn happened to be
    typed in, and a command naming a deleted room is the silent-127 class
    (`chatnode.helm_bin` is the same derivation the generated units use).

    `model` rides as `helm launch --model`, which carries it onto claude's
    argv: without it the pane took whatever model the credhome's settings.json
    defaulted to (task/2505 — a rehomed seat came up on sonnet)."""
    from . import chatnode
    argv = [chatnode.helm_bin(), "launch", "--seat", seat_name]
    if room:
        argv += ["--room", room]
    if model:
        argv += ["--model", model]
    return argv


def _ensure_instance_endpoint(family, seat_name):
    """None once this seat HAS its own allocated endpoint, else the refusal.

    THE COMMIT POINT OF THE ALLOCATION, here and deliberately not in the gate.
    A project-canonical instance's endpoint comes out of a durable ledger 100
    wide that nothing ever reclaims, so a slot may only be spent by a call that
    is about to MINT the assets carrying the port. AN ALLOCATOR REACHED FROM
    `_instance_gate` IS THE FAILURE MODE: `_spawn` calls that gate before the
    `--print` check, before the argument parse, before the model check and
    before the workspace proof, so a preview of a name, a typo'd flag or a
    refused workspace each takes a slot for a seat that is never created, and a
    hundred previews of a hundred names exhaust the span. Admission asks the
    PURE half (`_instance_endpoint_error`); this is the only caller that
    writes.

    Numbered and family seats derive their port and return None here untouched.
    """
    if family not in FAMILIES or seat_name == family:
        return None
    if _instance_port(family, seat_name) is not None:
        return None                      # already allocated: never reallocated
    if re.match(r"^%s-(\d+)$" % re.escape(family), seat_name):
        return None                      # derived from the suffix, not allocated
    _port, err = allocate_instance_port(seat_name)
    return err


def _spawn_native_plan(seat_name, project, room, cwd, role, model=None):
    """--print for the native leg: nothing spawned, reaped, re-minted — and
    NOTHING CREATED, which is why the env preview asks `build_env` not to
    allocate a scratch TMPDIR. The name is still READ BACK FROM ITS PRODUCER
    rather than restated from this function's own string."""
    from . import launch as launch_mod
    env = launch_mod.build_env(os.environ, seat_name, room=room,
                               allocate_scratch=False)
    print("helm seat spawn %s — plan (--print: nothing spawned, reaped, or "
          "re-minted):" % seat_name)
    print("  family: %s (NATIVE — no proxy, no port, no seat config, no "
          "launch.sh)" % NATIVE_FAMILY)
    print("  project: %s" % (project or "(none — numbered form)"))
    print("  cwd:   %s" % cwd)
    print("  env:   HELM_CHAT_NAME=%s HELM_CELL_PROFILE=%s "
          "(helm/launch.py build_env)"
          % (env["HELM_CHAT_NAME"], env["HELM_CELL_PROFILE"]))
    # THE SELECTED HOME IS PART OF THE PLAN, because it is what the seat's
    # session records — and every later exact-session proof about them — will
    # live under. A plan that showed the pane command but not its storage left
    # the operator unable to see WHERE this seat's sessions would be.
    # THE ONE ACCESSOR THE LIVE LEG USES, so the plan shows the home the child
    # will really get instead of a second derivation of it — and the launch line
    # below carries that same value, which is the whole of the pin.
    config_home = _native_config_home(env)
    print("  claude home: %s (its sessions/ is the exact-session proof)"
          % config_home)
    from . import cred
    fresh = cred.spawn_note(config_home)
    if fresh:
        print("  token: %s" % fresh)
    print("  role:  %s" % role)
    print("  model: %s" % (model or "(none — claude takes the home's settings "
                                    "default)"))
    print("  launch: %s" % _seat_lifecycle_impl._native_launch_command(
        _native_launch_argv(seat_name, room, model), role,
        config_home=config_home))
    print("  onboarding submit: %s" % _boot_brief_wire())
    return 0


def _native_config_home(env):
    """The Claude storage a native pane will REALLY write into, read out of the
    env its own producer built rather than assumed.

    THE PROOF MUST NOT READ A HOME NOBODY SELECTED. Every exact-session census in
    this tree looks under `<instance>/claude/sessions`, which is where a PROXY
    seat's launch.sh pins CLAUDE_CONFIG_DIR — so for a proxy seat that path IS
    the selected home. A native seat has no launch.sh: `helm launch` execs claude
    with whatever home the env names, which is the operator's `--home` pin, else
    an inherited CLAUDE_CONFIG_DIR, else claude's own default. Nothing selected
    the instance directory, so the census listed an empty directory and read zero
    live processes as proof the seat was dead. Pinning what the launch selected
    is what lets the census ask the storage the session actually uses.
    """
    from . import homes
    return env.get(homes.ENV_VAR["claude"]) or homes.DEFAULTS["claude"]


def _restore_spawn_register(d, prior):
    """Put a seat's register back as it was, after a native pre-spawn write whose
    pane then never started.

    THE PRE-SPAWN WRITE IS WHAT MAKES THIS NECESSARY, and it is not optional: the
    register has to exist before the pane can run its FIRST SessionStart, because
    that hook is the only authority for the session id and it silently skipped
    the binder when the record was not there yet. Existing early means it also
    exists on every path where the pane then fails, so each of those paths hands
    the seat back. `prior` None means there was no register at all, so the file is
    REMOVED — leaving one behind would name a pane nothing is running.

    RETURNS WHAT DISK DID: None when the restore (or the unlink) really happened,
    else the failure text. The warning below was the only trace of a swallowed
    failure, and the caller went on returning SETTLED_RESTORED — so the failure
    prose told the operator the seat had been handed back while this attempt's
    record was still on disk. A caller that has to report persistence needs the
    measurement, not a printed side effect.
    """
    from . import pk
    path = _spawn_path(d)
    try:
        if prior is None:
            if os.path.exists(path):
                os.unlink(path)
        else:
            pk.write_json(path, prior)
    except OSError as e:
        print("helm seat: WARNING — the prior spawn register %s could not be "
              "restored (%s); `helm seat where` may name a pane that never "
              "started" % (path, e), file=sys.stderr)
        return "%s" % e
    return None


def _pane_state_after_close(ad, handle):
    """(pane PROVEN absent, what the terminal proof says) about a pane helm just
    tried to close: (True, "CLOSED (...)") or (False, "UNKNOWN — ..."). Never
    "UP".

    "UP but unregistered" was printed unconditionally AFTER `stop_pane` had
    already returned proof of closure, so the one arm that succeeds in cleaning
    up announced the outcome it had just prevented — and an operator reading it
    goes hunting for a live pane, or worse, leaves one they believe is running.
    `stop_pane` answers in exactly three shapes (proven terminal, unproven,
    raised) and all three are rendered here, because the difference between
    "proven closed" and "could not be proven" is the whole content of the
    sentence.

    AND THE BOOL IS WHAT THE ROLLBACK DECIDES ON. Narrating the difference was
    only half of it: both outcomes then went to the same unconditional restore,
    which DELETES a fresh project seat's only record. The proof is returned beside
    the prose so `_settle_spawn_attempt` acts on the measurement instead of
    re-deriving it from a string.
    """
    try:
        from . import seat_exit_owner
        reason, unavailable = seat_exit_owner.stop_pane(ad, handle)
    except Exception as e:
        return False, "UNKNOWN — helm could not close it (%s)" % e
    if reason:
        return True, "CLOSED (%s)" % reason
    return False, "UNKNOWN — its close was not proven (%s)" % unavailable


# What `_settle_spawn_attempt` did, so the failure prose can DERIVE what
# `helm seat where` will now resolve instead of asserting it.
SETTLED_RESTORED = "restored"        # PROVEN absent: the prior register is back
SETTLED_INCOMPLETE = "incomplete"    # absence unproven: this attempt kept, INCOMPLETE
SETTLED_FOREIGN = "foreign"          # the record on disk is no longer this attempt's
SETTLED_UNRECORDED = "unrecorded"    # the INCOMPLETE stamp could not be written


def _settle_spawn_attempt(d, prior, attempt, absent, why, retained=None):
    """Close out a spawn attempt that produced no registered pane — handing the
    seat back ONLY when the pane is PROVEN ABSENT. Returns
    (SETTLED_*, record, unpersisted):

      SETTLED_*     which BRANCH settled the attempt;
      record        what `helm seat where` will resolve from here on — READ FROM
                    DISK whenever a write failed, never the dict helm meant to
                    write, and None when disk held nothing OR could not be read
                    (which of the two is in `unpersisted["disk"]`);
      unpersisted   None when disk holds what this settlement intended, else the
                    MEASUREMENT of what did not persist: {"outcome":
                    "unpersisted"|"restore-failed", "error": <the OSError text>,
                    "intended": <the record helm meant disk to hold>, "disk":
                    "record"|"absent"|"unknown" — which of the three answers the
                    re-read got, "disk_error": <why the re-read failed, or None>}.

    PERSISTENCE IS MEASURED, NOT ASSUMED, and assuming it is the finding. The
    UNKNOWN branch mutated the re-read record with the known handle and an
    INCOMPLETE attempt, and when `pk.write_json` then raised it returned that
    MUTATED dict beside SETTLED_UNRECORDED — so `_settled_where_claim` described
    "the record on disk" using an attempted handle and a state that never reached
    disk. The proven-absent branch returned SETTLED_RESTORED whether or not the
    restore or the unlink had been swallowed. Both are one root: a return value
    that reported an INTENTION in the register of a fact. Disk is re-read after a
    failed write, the intention travels separately and is rendered as an
    intention, and the restore reports its own failure.

    ABSENCE AND UNKNOWN ARE DIFFERENT OUTCOMES, and collapsing them is the
    finding. The register goes down before the child can run, because the first
    SessionStart is the only authority for the session id and it needs a record to
    bind into; that makes every failure path a decision about AUTHORITY, and the
    first cut made the same decision on both. For a fresh project seat `prior` is
    None, so "restore" DELETES the only record of its family, its project, its
    pane and its ownership — including when the close was UNKNOWN and the pane may
    still be running the command helm just handed it. The operator is then left
    with a live pane no verb can name, and the next spawn of that name sees an
    empty slot and adds a second one.

    So: PROVEN absent restores exactly what was there. UNKNOWN keeps THIS
    attempt's record and stamps it INCOMPLETE with the reason — a truthful record
    of a pane whose absence helm could not prove, with no session and no ownership
    proof invented for it, in a named state a later reconciliation can resolve.

    AND IT KEEPS EVERY FACT THE CALLER ALREADY HOLDS. The record on disk was
    published BEFORE the child existed, so it says handle None (or pid None) —
    and the first cut re-read it and changed only the attempt, discarding the
    handle the adapter had returned and the pid the launch had produced. The
    INCOMPLETE record then named a pane it could not point at: the resolver
    answered "no pane identity", `where` reported GONE from a pid of None, and the
    one thing an operator needs from an incomplete attempt — WHICH pane — was
    exactly what it had thrown away. `retained` is those facts (the returned
    handle; the pid and its birth identity), written where they are known and
    never as None over a value the child's own hook may already have written.
    The pane legs and the headless leg all settle through this one function.

    It writes only where the attempt on disk is still this one: an abandoned
    attempt that something else has since replaced is not ours to stamp.
    """
    from . import pk

    def reread():
        """What disk says after a write helm could not complete, as one of THREE
        answers: (record, "record"|"absent"|"unknown", read failure text).

        THE RE-READ CAN FAIL TOO, and the first cut could not say so. It re-read
        through `_spawn_record`, which answers None for a missing file AND for a
        file it could not open or parse — so when the storage was UNAVAILABLE
        (the same unavailability that had just refused the write) the claim below
        rendered None as "no record: none is on disk", asserting an absence
        nothing had measured. A settlement that reports persistence has to
        distinguish "read it, this is what it holds" from "read it, nothing is
        there" from "could not read it at all"; only the first two license a
        sentence about the register's contents.

        `strict=True` is what separates them: a missing file still returns the
        default, an unopenable or unparseable one RAISES. A JSON value that is
        not an object is counted unreadable rather than absent — helm cannot
        describe a register it cannot interpret.
        """
        try:
            found = pk.read_json(_spawn_path(d), None, strict=True)
        except Exception as e:
            return None, "unknown", "%s" % e
        if found is None:
            return None, "absent", None
        if not isinstance(found, dict):
            return None, "unknown", ("the register is not a JSON object (%s)"
                                     % type(found).__name__)
        return found, "record", None

    if absent:
        failure = _restore_spawn_register(d, prior)
        if failure is None:
            return SETTLED_RESTORED, prior, None
        # The restore (or the unlink) did not happen: what `where` resolves is
        # whatever the failed write left, which is re-read here — and the re-read
        # reports its OWN outcome, because it can fail for the same reason.
        left, disk, disk_error = reread()
        return (SETTLED_RESTORED, left,
                {"outcome": "restore-failed", "error": failure,
                 "intended": prior, "disk": disk, "disk_error": disk_error})
    rec, disk, disk_error = reread()
    if disk == "unknown":
        # THE SAME ROOT ONE LINE OVER: helm cannot prove the attempt on disk is
        # still this one, and it could not prove it is gone either. Reporting
        # this as SETTLED_FOREIGN with no measurement printed "the record on
        # disk is gone" about a register nothing read.
        return (SETTLED_FOREIGN, None,
                {"outcome": "unread", "error": disk_error, "intended": None,
                 "disk": disk, "disk_error": disk_error})
    if not rec or (rec.get("attempt") or {}).get("id") != attempt["id"]:
        return SETTLED_FOREIGN, rec, None
    for field, value in (retained or {}).items():
        if value is not None:
            rec[field] = value
    rec["attempt"] = dict(attempt, state=SPAWN_ATTEMPT_INCOMPLETE, reason=why)
    try:
        pk.write_json(_spawn_path(d), rec)
    except OSError as e:
        print("helm seat: WARNING — %s's INCOMPLETE spawn attempt could not be "
              "recorded (%s), and its pane's absence was never proven; look for "
              "a pane nothing names" % (rec.get("seat"), e), file=sys.stderr)
        # `rec` is the INTENTION. Disk is re-read so the claim below describes
        # what an operator will actually find there — and when THAT read fails
        # too the claim says the disk state is unknown instead of inventing an
        # absence out of a reader that answered None.
        held, disk, disk_error = reread()
        return (SETTLED_UNRECORDED, held,
                {"outcome": "unpersisted", "error": "%s" % e, "intended": rec,
                 "disk": disk, "disk_error": disk_error})
    return SETTLED_INCOMPLETE, rec, None


def _settled_record_ref(rec):
    """One record, as `helm seat where` will name it: its process or pane, its
    attempt and that attempt's state, and its session or the word unbound."""
    attempt = rec.get("attempt") or {}
    ref = ("pid %s" % rec.get("pid")) if rec.get("harness") == "headless" \
        else ("%s pane %s" % (rec.get("harness"), rec.get("handle")))
    return "%s, attempt %s %s, session %s" % (
        ref, attempt.get("id"), attempt.get("state") or "(no attempt)",
        rec.get("session") or "unbound")


def _settled_where_claim(seat_name, settled):
    """What `helm seat where <seat>` WILL resolve after a settlement — derived
    from what `_settle_spawn_attempt` returned, never asserted.

    The first cut printed "`helm seat where` resolves nothing" on every failed
    final register, including the ones whose settlement had just KEPT an
    INCOMPLETE record with a pane in it — the sentence was written for the
    proven-absent branch and reused on the other. A sentence about what a verb
    will answer has to be built from the record that verb reads.

    AND ONLY FROM WHAT DISK HOLDS. When the settlement measured a failed write it
    hands back both halves — what disk actually holds and what helm INTENDED —
    and the two are rendered apart: the verb's answer is the disk half, and the
    intended handle and state are named UNPERSISTED. Describing the intention as
    the verb's answer is how an operator was sent to `helm seat where` for a pane
    identity no record carried.

    AND THE DISK HALF HAS THREE ANSWERS, NOT TWO. The re-read after a failed
    write can itself fail — the storage that refused the write is the same
    storage the re-read asks — and a reader that answers None for "not there"
    and None for "could not look" made this sentence say "no record: none is on
    disk" about a register helm never read. `unpersisted["disk"]` carries which
    of the three happened and the unknown leg says so in those words, naming the
    read's own error and asserting nothing about a handle, a state or an absence.

    `helm seat where --json` is not rendered from here: it re-reads spawn.json
    itself (`seat_lifecycle._where`), so its answer about an unreadable register
    is that verb's own and is left alone.
    """
    state, rec, unpersisted = settled
    verb = "`helm seat where %s`" % seat_name
    if unpersisted:
        disk = unpersisted.get("disk")
        if disk == "unknown":
            # THE READ FAILED, so this sentence has nothing to say about the
            # register's contents and must not pretend otherwise. Saying "none
            # is on disk" here reported an unreadable store as an empty one.
            held = (" resolves whatever is on disk, and the disk state could "
                    "NOT be read to say what that is (%s)"
                    % unpersisted.get("disk_error"))
        elif rec:
            held = (" resolves the record on disk, UNCHANGED by this "
                    "settlement: " + _settled_record_ref(rec))
        else:
            held = " resolves no record: none is on disk"
        if unpersisted["outcome"] == "unread":
            return (verb + held + ". helm did NOT stamp this attempt: it could "
                    "not read the register to see whether the attempt on disk "
                    "is still this one")
        intended = unpersisted.get("intended")
        if isinstance(intended, dict):
            intent = (" helm INTENDED, and did NOT persist: "
                      + _settled_record_ref(intended))
        elif disk == "unknown":
            intent = (" helm INTENDED to leave no register of this attempt, and "
                      "could not read disk to say whether one is there")
        elif rec:
            intent = (" helm INTENDED to leave no register of this attempt, and "
                      "did NOT: the file is still there")
        else:
            intent = (" helm INTENDED to leave no register of this attempt, and "
                      "disk holds none — but the write that would have removed "
                      "it failed, so nothing here proves helm removed it")
        if unpersisted["outcome"] == "restore-failed":
            return (verb + held + ". The prior register could NOT be put back "
                    "(%s), so the seat was NOT handed back;%s"
                    % (unpersisted["error"], intent))
        return (verb + held + ". The INCOMPLETE stamp could NOT be written "
                "(%s), so the attempted pane identity below is an INTENTION and "
                "not a record;%s" % (unpersisted["error"], intent))
    if state == SETTLED_RESTORED:
        if not rec:
            # No register of this attempt remains. `where` may still answer
            # from an archived terminal record or the adoption seam — that is
            # its business; the claim here is about THIS attempt's register.
            return (verb + " resolves no register of this attempt: none "
                    "existed before it and its pane is PROVEN absent")
        return verb + " resolves the prior register it restored: " \
            + _settled_record_ref(rec)
    if state == SETTLED_INCOMPLETE:
        return verb + " resolves this attempt's INCOMPLETE record: " \
            + _settled_record_ref(rec)
    if state == SETTLED_FOREIGN:
        if not rec:
            return (verb + " resolves nothing: the record on disk is gone and "
                    "was not this attempt's to restore")
        return verb + " resolves a register another attempt owns: " \
            + _settled_record_ref(rec)
    # SETTLED_UNRECORDED always arrives with a measurement above; this is the
    # unreachable-by-construction tail kept honest rather than deleted.
    return (verb + " resolves the record on disk, whose attempt helm could not "
            "stamp INCOMPLETE (see the warning above): "
            + (_settled_record_ref(rec) if rec else "nothing"))


def _refuse_in_flight_spawn(d):
    """True (having said why) when another process's PENDING attempt owns this
    seat — a LIVE one, or one whose process helm cannot verify. The door every
    mutating verb knocks on before its first write (spawn's allocation and reap,
    resume's manual re-mint and reap alike), because the lifecycle lock is
    deliberately released while a child starts and only the attempt can say."""
    conflict = _pending_attempt_conflict(_spawn_record(d))
    if not conflict:
        return False
    print("helm seat: " + conflict, file=sys.stderr)
    return True


def _spawn_native(seat_name, project, d, room, cwd, role, replace, ad,
                  model=None):
    """`seat spawn <project>-claude` — ONLY WHAT NATIVE DIFFERS IN.

    NOT A SECOND LIFECYCLE, and that is the point. Everything a spawn owes its
    seat whatever its family — the per-seat lifecycle lock, the prior register,
    the stale reap and the --replace refusal, the surface-ownership proof, the
    home provision, the onboarding submit and the lead posture — is `_spawn`'s
    and the shared rungs it calls, reached through the same doors the proxy leg
    reaches them through. What is left here is the actual difference: a native
    seat has no proxy, no port, no cred to translate and no launch.sh, because
    its identity is exported by `helm launch --seat`, the one native writer of
    HELM_CHAT_NAME.
    """
    if ad is None:
        print("helm seat: %s is a native claude seat and no metaharness is "
              "here to hold its pane — run this in the pane that will BE the "
              "seat:\n  %s"
              % (seat_name, _seat_lifecycle_impl._native_launch_command(
                  _native_launch_argv(seat_name, room, model), role)),
              file=sys.stderr)
        return 1
    from . import harness
    from . import launch as launch_mod
    from . import pk
    # READ THE NAME BACK FROM ITS PRODUCER, never from this function's own
    # string: what the child will carry is whatever build_env writes.
    env = launch_mod.build_env(os.environ, seat_name, room=room)
    # ONE ACCESSOR, TWO CONSUMERS — the record below and the command above it,
    # from the same call. Reading the home for the RECORD while leaving it out of
    # the COMMAND makes the recorded proof home and the child's actual home two
    # independent values that agree only when the pane happens to inherit the
    # same CLAUDE_CONFIG_DIR. Reading it once and feeding both is what makes
    # "recorded equals actual" a property of the code rather than of the terminal.
    config_home = _native_config_home(env)
    # THE PANE'S SYNC LINE PRINTS INSIDE THE PANE, so the spawn says up front
    # what that launch will find: a credhome stale against Orca, and whether it
    # can sync (a home this spawner's own claude holds cannot).
    from . import cred
    fresh = cred.spawn_note(config_home)
    if fresh:
        print("helm seat: %s claude home %s — %s" % (seat_name, config_home, fresh))
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
    except OSError as e:
        print("helm seat: cannot record %s (%s): %s" % (seat_name, d, e),
              file=sys.stderr)
        return 1
    prior = _spawn_record(d)
    rec = {"v": 1, "seat": seat_name, "identity": seat_name,
           "family": NATIVE_FAMILY, "backend": "native", "project": project,
           "role": role, "worktree": cwd, "room": room,
           "room_source": "explicit" if room else None,
           "harness": ad.name, "handle": None,
           "config_home": config_home,
           "launch_sh": None, "model": model, "ts": pk.now_ts(),
           "session": None}
    # THE REGISTER GOES DOWN BEFORE THE PANE CAN RUN — the producer-authoritative
    # half of the session handshake. A pane's FIRST SessionStart is the only
    # authority for its session id, and every consumer of it resolves the seat's
    # family through the seat's own register (`registered_seat_family`): with no
    # register yet, `seats_join` could not resolve the family, skipped the binder
    # entirely, and that first binding was simply dropped — the record then sat
    # at session None with nothing scheduled to repair it. Writing first means
    # the hook always has an authority to bind into; it shares this seat's
    # lifecycle lock, so its write waits for this spawn instead of racing it.
    # THE ATTEMPT TOKEN IS MINTED HERE, by the one producer, and the command
    # below is built AFTER it so the child's launch line carries it.
    attempt = _publish_spawn_attempt(seat_name, seat_name, d, rec)
    if attempt is None:
        # NOTHING HAS STARTED, so the pane is absent by construction — the one
        # rollback on this leg that needs no proof.
        _restore_spawn_register(d, prior)
        print("helm seat: %s could not be registered, so NOTHING was spawned — "
              "a native pane whose register does not exist cannot bind its own "
              "session or be found for the next duplicate reap"
              % seat_name, file=sys.stderr)
        return 1
    command = _seat_lifecycle_impl._native_launch_command(
        _native_launch_argv(seat_name, room, model), role,
        config_home=config_home, token=attempt["id"])
    # THE LOCK COMES OFF FOR THE CHILD. Everything from here to the finalize
    # WAITS ON THE PANE — the adapter create, the pane-boot grace and the
    # onboarding submit — and the seat's own first SessionStart runs inside that
    # window under a 5s hook budget, taking this very lock to bind its session and
    # initialize its room cursors. Holding it across the send delay could get the
    # child's own hook killed mid-bind, which is a seat that starts deaf and
    # unbound. The PENDING attempt published one line up is what keeps another
    # spawn or resume out meanwhile.
    handle, spawn_error, create_absent = None, None, False
    submit_error, onboard_state, onboard_proof = None, None, None
    with _seat_lifecycle_impl._seat_lifecycle_lock_released(d):
        try:
            handle = ad.spawn(command, title=seat_name, cwd=cwd)
        except Exception as e:
            spawn_error = e
            # THE SAME ABSENCE QUESTION THE PROXY LEG ALREADY ASKS. An adapter
            # that proves its create never started leaves no pane; one whose
            # reply merely failed may have created a remote pane first, and
            # calling that absent throws away the only record of it.
            create_absent = bool(getattr(
                ad, "spawn_failure_absent", lambda _e: False)(e))
        if handle is not None:
            try:
                onboard_state, onboard_proof = _spawn_submit(
                    ad, handle, seat_name)
            except harness.HarnessError as e:
                submit_error = e
    if spawn_error is not None:
        _settle_spawn_attempt(
            d, prior, attempt, create_absent,
            "the %s adapter's pane create failed (%s) and no handle exists to "
            "prove the pane absent" % (ad.name, spawn_error))
        print("helm seat: %s pane spawn failed: %s%s"
              % (ad.name, spawn_error, "" if create_absent else
                 "; the adapter may have created a pane before its reply "
                 "failed, so %s's record is KEPT as an incomplete attempt "
                 "rather than removed" % seat_name), file=sys.stderr)
        return 1
    if submit_error is not None:
        closed, state = _pane_state_after_close(ad, handle)
        settled = _settle_spawn_attempt(
            d, prior, attempt, closed,
            "onboarding failed (%s) and pane %s is %s"
            % (submit_error, handle, state), retained={"handle": handle})
        print("helm seat: %s spawn via %s failed: %s; incomplete pane %s is %s "
              "— %s"
              % (seat_name, ad.name, submit_error, handle, state,
                 _settled_where_claim(seat_name, settled)), file=sys.stderr)
        return 1
    final = _finalize_spawn_attempt(seat_name, seat_name, d, rec, attempt,
                                   handle=handle)
    if final is None:
        closed, state = _pane_state_after_close(ad, handle)
        # THE HANDLE IS RETAINED and the where-claim is DERIVED from what the
        # settlement returned: on UNKNOWN the kept record names pane `handle`,
        # and that is what `where` will resolve — not "nothing".
        settled = _settle_spawn_attempt(
            d, prior, attempt, closed,
            "the pane could not be recorded and pane %s is %s"
            % (handle, state), retained={"handle": handle})
        print("helm seat: %s could not record pane %s, which is now %s — %s"
              % (seat_name, handle, state,
                 _settled_where_claim(seat_name, settled)), file=sys.stderr)
        return 1
    rec = final
    # THE SESSION BINDING IS BACKFILLED, the same rung the proxy leg runs: the
    # hook may already have fired (and waited on the lock), and where the
    # metaharness can prove which live process owns this exact pane, the spawn
    # binds the session itself rather than leaving the record at None and hoping.
    session_warning = None
    # Asked of the adapter's own NAME, which is the same key the record carries
    # and the same one `_backfill_spawn_session` requires of it — so the
    # condition here and the function's own precondition cannot drift apart.
    if getattr(ad, "name", None) == "orca" and not \
            _backfill_spawn_session(seat_name, d, ad):
        session_warning = ("pane session identity is not yet proven; "
                           "SessionStart must bind it before autocompact can "
                           "act")
    elif not (_spawn_record(d) or {}).get("session"):
        # SAY WHAT IS SUPPORTED TODAY, NOT WHAT WOULD BE NICE. Promising that
        # "the session binds at the pane's first SessionStart" for EVERY harness
        # is a claim about a binder that
        # (`_sessionstart_pane_fields`) supports exactly the harnesses it declares
        # and REFUSES every other, Herdr among them. So a successful Herdr launch
        # printed an affirmative recovery that nothing in the tree can perform,
        # and the record stayed at session None with no leg scheduled to repair
        # it. The capability is asked of the binder's own declaration rather than
        # restated here, so the two cannot drift.
        session_warning = (
            "%s cannot prove which live process owns this pane, so the session "
            "binds at the pane's first SessionStart" % ad.name
            if ad.name in SESSION_BINDING_HARNESSES else
            "%s cannot prove which live process owns this pane, and helm cannot "
            "bind a session from a %s pane's SessionStart either (it proves a "
            "pane's own session for: %s). This seat's register stays at session "
            "None, so autocompact stays fail-closed for it until something else "
            "proves the session" % (ad.name, ad.name,
                                    ", ".join(SESSION_BINDING_HARNESSES)))
    if session_warning:
        print("helm seat: WARN — " + session_warning, file=sys.stderr)
    # THE OUTCOME BECOMES STATE BEFORE IT BECOMES A SENTENCE — the same door
    # the proxy leg writes through, so one reader answers for both families.
    _record_onboarding(d, seat_name, onboard_state, onboard_proof)
    if onboard_state == harness.DELIVERED:
        print("helm seat: spawned %s via %s — pane %s, cwd %s; identity "
              "exported by `helm launch --seat %s` (HELM_CHAT_NAME=%s, claude "
              "home %s); onboarding submitted (beacon-arm + @%s work)"
              % (seat_name, ad.name, handle, cwd, seat_name,
                 env["HELM_CHAT_NAME"], rec["config_home"], seat_name))
        return 0
    print("helm seat: spawn of %s via %s INCOMPLETE — pane %s is up and "
          "REGISTERED (do not re-spawn), but its onboarding brief is NOT "
          "PROVEN submitted (%s: %s). Read the pane: if the brief is sitting "
          "unsent in the composer, submit it; the seat is otherwise blank."
          % (seat_name, ad.name, handle, onboard_state, onboard_proof),
          file=sys.stderr)
    return 1


def _spawn(seat_name, rest, _locked=False):
    """seat spawn <seat> [--room R] [--cwd DIR] [--model M]
    [--role worker|lead] [--replace] [--print] — see
    comment above for the three paths + the common laws. Live replacements are
    serialized per seat so concurrent callers cannot both reap an empty slot,
    launch duplicates, and race the one spawn.json register."""
    family, project, err = spawn_identity(seat_name)
    if err:
        print("helm seat: " + err, file=sys.stderr)
        return 2
    if not _locked:
        # SAID ONCE, on the first (unlocked) pass — the locked re-entry below
        # is the same spawn, not a second one.
        if project:
            print("helm seat: %s is project %s's %s seat "
                  "(project-canonical name: <project>-<family>)"
                  % (seat_name, project, family))
        notice = _project_tla_notice(seat_name, family, project)
        if notice:
            print("helm seat: " + notice)
    native = family == NATIVE_FAMILY
    # ONE GATE FOR EVERY FAMILY. `_instance_gate` is the PROXY-specific
    # predicate (mode, instance number, allocated endpoint) wrapped around
    # `_seat_spawn_gate`, the family-agnostic surface-OWNERSHIP proof; native
    # knocks on that shared door directly, because it is the only half that
    # applies to it and it is the half that must never be skipped — an instance
    # directory symlinked at a sibling makes the register write land on the
    # sibling's spawn.json, and containment under seats_root is not ownership.
    # (Instance-spawn gate, a MED finding — it lived only on `launch`, so
    # `spawn kimi-2` / `spawn codex-1` minted launch lines pointed at a SIBLING
    # family's port range. ONE shared predicate with `_resume` too, the second
    # MED finding: the gate on spawn alone let resume mint the refused seats.)
    gate = _seat_spawn_gate(family, seat_name) if native \
        else _instance_gate(family, seat_name)
    if gate:
        print("helm seat: " + gate, file=sys.stderr)
        return 2
    d = _instance_dir(family, seat_name)
    launch_sh = os.path.join(d, "launch.sh")
    if not native and not os.path.exists(launch_sh) and \
            not os.path.exists(os.path.join(seat_dir(family), "config.yaml")):
        print("helm seat: no %s seat minted — `helm seat add %s` first, then "
              "`helm seat spawn %s`" % (seat_name, family, seat_name),
              file=sys.stderr)
        return 1
    # PARSE PURE, ALWAYS. The home worktree is created exactly once, inside the
    # per-seat spawn lock below and AFTER the project-scope door — the unlocked
    # pass exists only to surface an argument error early, and the dry-run pass
    # must stay side-effect free.
    parsed, arg_err = _spawn_args(rest, seat_name, provision=False)
    if arg_err:
        print("helm seat: %s; usage: helm seat spawn <seat> [--room R] "
              "[--cwd DIR] [--model M] [--role worker|lead] "
              "[--replace] [--print]" % arg_err, file=sys.stderr)
        return 2
    room, cwd, dry_run, replace, model, role = parsed
    if project:
        # THE WORKSPACE MUST BELONG TO THE PROJECT THE NAME CLAIMS, and this is
        # the one place that can still be true: below here a home worktree and a
        # seat branch get created in whatever repository the reference resolves
        # to. Numbered seats are untouched — they claim no project, so there is
        # nothing to contradict.
        scope_err = _spawn_scope_error(
            seat_name, project, cwd, _spawn_workspace_ref(rest, cwd))
        if scope_err:
            print("helm seat: " + scope_err, file=sys.stderr)
            return 2
    if not native:
        from .seat_catalog import proxy_runtime_model_error
        model_error = proxy_runtime_model_error(
            family, model or _persisted_model(d, seat_name))
        if model_error:
            print("helm seat: " + model_error, file=sys.stderr)
            return 2
    if not dry_run and not _locked:
        with _seat_lifecycle_lock(d):
            return _spawn(seat_name, rest, _locked=True)
    if _locked and "--cwd" not in rest:
        # PROVISION THE DEFAULT HOME, once, in the lock, for every family. A
        # LIVE call that resolves with provision=False hands the adapter a
        # prospective path that does not exist, and the adapter's
        # `cd PATH && exec` then fails in the pane — the seat cannot start at
        # the checkout its own docs advertise. A dry run
        # never reaches here, so the plan stays side-effect free.
        cwd = _seat_home_cwd(seat_name, provision=True)
    if native:
        from . import harness
        ad = harness.detect()
        # THE MODEL IS STICKY, read BEFORE the reap: an explicit --model wins,
        # else the seat's own register carries the one it last launched on, so
        # `--replace` relaunches on the same model instead of the credhome's
        # settings default (the proxy leg's `_persisted_model` law).
        model = model or _persisted_model(d, seat_name)
        if dry_run:
            return _spawn_native_plan(seat_name, project, room, cwd, role, model)
        # BEFORE THE REAP, because a reap destroys the runtime a concurrent spawn
        # is still building. The lifecycle lock alone no longer answers this: it
        # is released on purpose while a child starts, so the PENDING attempt is
        # what says "someone else is mid-spawn of this seat right now".
        if _refuse_in_flight_spawn(d):
            return 1
        if _spawn_reap(seat_name, d, ad, replace):
            return 1
        rc = _spawn_native(seat_name, project, d, room, cwd, role, replace, ad,
                           model)
        if rc == 0:
            _ensure_autocompact_timer()
        return rc
    prior = _spawn_record(d) or {}
    room_worktree = prior.get("room_worktree") or prior.get("worktree")
    # Room provenance rides with the room (roster-scatter class): an explicit
    # --room / launch.sh HELM_CHAT_ROOM is explicit; a launch.sh room the
    # seam stamped derived stays derived; NO room stays None — the roster
    # mirror must not invent a 'main' home the SessionStart join would never
    # have written (seats.resolve_homing derives the real one from cwd).
    room_source = "explicit" if room is not None else None
    if room is None:
        room, room_source = _homing_from_launch(launch_sh)
        room_source = room_source or ("explicit" if room else None)
        # A DERIVED ROOM IS A FUNCTION OF THE CWD, so moving the cwd makes it
        # stale BY ITS OWN DEFINITION — the provenance field records exactly
        # what invalidates it, and nothing was reading it that way. The room in
        # launch.sh describes where the seat lived at its LAST mint; --cwd says
        # where it will live now.
        #
        # Measured 2026-08-15, live, on another project: the kimi seat was
        # re-spawned with --cwd .../<project> while its launch.sh carried
        # HELM_CHAT_ROOM=helm and HELM_CHAT_ROOM_SOURCE=derived. It kept the
        # helm room — so it did that project's work while homed in helm's
        # coordination traffic, and that project's conversation SPLIT ACROSS TWO
        # ROOMS: 107 rows where the proxy families posted, 33 where the seat
        # reading explicitly was posting. The operator saw it as messages
        # vanishing and went to the raw store to read them.
        #
        # EXPLICIT SURVIVES, DERIVED DOES NOT. An operator who set --room meant
        # it and a later --cwd does not overrule them; a derivation carries no
        # such intent, only a stale input.
        # THE PREDICATE IS THE FLAG, NOT THE RESOLVED VALUE. `cwd` is ALWAYS
        # populated — _spawn_args defaults it to the seat's own home worktree —
        # so `if cwd` is true on every spawn and re-derived rooms that had not
        # moved at all. That broke two standing contracts on the first gate
        # (a derived launch room must survive a plain re-spawn, and must carry
        # its provenance into the reminted script), and both were right: a
        # re-spawn IN PLACE is not a move and must change nothing.
        #
        # DERIVE FROM THE CWD ALONE, NEVER THROUGH THE HOMING RESOLVER. The
        # first cut called resolve_homing(None, cwd), which honours the ENV
        # SEAM before the cwd — so from inside any helm seat (where spawns
        # actually happen) the SPAWNING process's own HELM_CHAT_ROOM won over
        # the target cwd, and an ambient room with no _SOURCE read as
        # explicit. A second probe measured it: --cwd /new-project returned
        # old-project with the ambient set, new-project without. My probe,
        # my arms (which mocked the resolver) and the gate all ran from
        # contexts with NO ambient room — every green shared the one property
        # production lacks. derive_home_room is the cwd-only leg.
        #
        # AND A MEASURED NONE CLEARS THE STALE ROOM — durably, as `cleared`.
        # A cwd that is valid but projectless derives nothing; `if fresh` kept
        # the old derived room in that case, which is the stale-carry-forward
        # this whole fix exists to stop. Explicit is preserved by the branch
        # above; derived follows the cwd wherever it goes, including to
        # nowhere. Writing that clear as a bare None made it indistinguishable
        # from a seat that never had a room, and the difference decides
        # whether the NEXT resume may adopt an ambient one.
        #
        # AND AN UNREADABLE DERIVATION IS NOT A CLEAR. Both of those live in
        # _room_for_cwd now, shared with resume, because two copies of one law
        # is how the paths diverged in the first place.
        room, room_source, room_worktree = _room_for_cwd(
            seat_name, cwd, room, room_source,
            _cwd_moved(prior, seat_name, cwd, "--cwd" in rest),
            room_worktree)
    multi = _multi_from_launch(launch_sh)
    # Resolve before every identity consumer, including the first-turn prompt
    # and dry-run plan. The storage label selects paths; it never self-identifies
    # a process after a durable rename.
    from .seat_launch_assets import _launch_identity
    identity, identity_error = _launch_identity(seat_name)
    if identity_error:
        print("helm seat: REFUSED — launch identity for %s is unresolved: %s; "
              "the existing pane and launch asset are unchanged"
              % (seat_name, identity_error), file=sys.stderr)
        return 1
    onboard = onboarding_prompt(identity, room, role=role)
    from . import harness
    ad = harness.detect()
    if dry_run:
        return _spawn_plan(
            identity, d, launch_sh, room, cwd, onboard, ad,
            replace=replace, role=role, pane_onboard=_boot_brief_wire(),
            storage_seat=seat_name)
    # THE SAME IN-FLIGHT DOOR THE NATIVE LEG AND RESUME KNOCK ON, in the same
    # place: before the allocation, the reap and the re-mint — before this verb's
    # first write of any kind. A reap destroys the runtime a concurrent spawn is
    # still building, and the lifecycle lock is released on purpose while a
    # child starts, so only the PENDING attempt can say another spawn owns it.
    if _refuse_in_flight_spawn(d):
        return 1
    # ALLOCATE THE ENDPOINT NOW: past every refusal — the plan above, the
    # argument parse, the model check, the workspace proof, the identity gate,
    # the in-flight attempt — and immediately before the reap and the re-mint,
    # which are the acts that write this seat's port into its launch line and
    # its proxy config. Before the REAP on purpose: a refusal here must leave
    # the live pane it would have replaced alone.
    endpoint_err = _ensure_instance_endpoint(family, seat_name)
    if endpoint_err:
        print("helm seat: %s cannot be spawned — %s"
              % (seat_name, endpoint_err), file=sys.stderr)
        return 1
    # EVERY REFUSAL THAT READ-ONLY EVIDENCE CAN ANSWER IS ANSWERED BEFORE THE
    # REAP. The reap stops the live pane and archives its register, and neither
    # can be taken back, so a refusal discovered after it has destroyed a
    # working seat in order to say no. The launch script's readability and the
    # surface the re-mint will write are both knowable now; the writer below
    # asks the same surface question again at its own first write.
    launch_was = _launch_snapshot(launch_sh)
    if launch_was[0] == "unknown":
        print("helm seat: refusing to re-mint unreadable launch script %s; "
              "rollback could not restore bytes it cannot capture" % launch_sh,
              file=sys.stderr)
        return 1
    from .seat_launch_assets import _launch_surface_refusal
    surface_refusal = _launch_surface_refusal(family, seat_name, d)[0]
    if surface_refusal:
        print("helm seat: " + surface_refusal, file=sys.stderr)
        return 1
    # Same pre-reap identity gate as resume: a refusal must leave the current
    # pane and launch asset alive, not discover the conflict after replacement
    # has already destroyed the runtime it was meant to refresh.
    if _spawn_reap(seat_name, d, ad, replace):
        return 1
    # mint hygiene AFTER the reap (a stale pane's sh may still be reading the
    # old launch.sh — the resume-verb ordering law), workdir=cwd so the trust
    # seed covers where the seat will actually run.
    # room_source rides INTO the re-minted script (HELM_CHAT_ROOM_SOURCE):
    # dropping it here laundered a derived room to explicit — the child's
    # SessionStart join then outranked (and overwrote) an operator-set home.
    #
    # AND A SPAWN THAT FAILS FROM HERE ON MUST LEAVE THE SEAT UNCHANGED. This
    # re-mint rewrites the one script every later `launch`/`resume` reads, and
    # it happens BEFORE the pane exists, so every failure below used to leave
    # the seat carrying the room, workdir and env of a pane that never
    # started. Measured: an adapter that refuses the pane moved launch.sh from
    # ('proj-x', 'derived') to un-homed and returned 1 -- and the next resume
    # of that seat then took the room of whatever process ran it. The snapshot
    # is restored on every return that leaves NO SURVIVING pane; once a pane is
    # up — registered, or merely not PROVABLY closed — the spawn has really
    # happened for that pane and the new script is the one it is running. The
    # snapshot itself was taken above the reap: the reap does not write the
    # script, so the bytes are the same ones and an unreadable script refuses
    # while the old pane is still alive.

    def _unwound(survivor=None):
        _restore_launch(launch_sh, launch_was, survivor)
        return 1
    try:
        refused = _write_launch_assets(
            family, d, room, seat_name, workdir=cwd,
            room_source=room_source, multi=multi,
            model=model, identity=identity) is _SEAT_SURFACE_REFUSED
    except OSError as e:
        print("helm seat: launch asset re-mint failed: %s" % e,
              file=sys.stderr)
        return _unwound()
    if refused:
        return _unwound()
    # per-instance proxy fate: an INSTANCE seat owns its OWN proxy
    # (instances/<seat>/), so spawn mints + starts THAT seat's proxy — never
    # the family's. MINT FIRST (HIGH): a never-launched instance has
    # no config yet, and gating on its existence silently skipped BOTH the
    # auto-start AND the WARN — a spawn-first codex-2 launched DEAD (launch.sh
    # pointed at 8319, empty token, no proxy) where pre-lane it WORKED on the
    # shared family proxy. `_mint_instance_proxy` is idempotent, so minting
    # here mirrors `launch` and is a no-op for an already-minted instance.
    fam = FAMILIES[family]
    if seat_name != family and fam["mode"] == "proxy":
        _mint_instance_proxy(family, seat_name)
    seat_cfg = os.path.join(_proxy_home(family, seat_name), "config.yaml")
    if os.path.exists(seat_cfg) and not _running_pid(family, seat_name):
        if _up(family, quiet=True, seat=seat_name) == 0:
            print("  (proxy was down — auto-started)")
        else:
            print("helm seat: WARN — %s proxy not running and auto-start "
                  "failed; the seat errors until `helm seat up %s`"
                  % (seat_name, seat_name), file=sys.stderr)
    from . import pk
    # `model` and `role` are PERSISTED, not just minted: launch.sh is
    # REFRESHED by every launch/resume/add, and each writer must re-derive the
    # explicit model while resume reasserts lead posture. Dropping either
    # silently turns a spark seat back into sol or a lead back into a worker.
    prior_rec = _spawn_record(d)
    rec = {"v": 1, "seat": seat_name, "identity": identity,
           "family": family, "project": project,
           "role": role, "worktree": cwd,
           "room": room, "room_source": room_source,
           "room_worktree": room_worktree, "launch_sh": launch_sh,
           "model": model, "ts": pk.now_ts(), "session": None}
    if ad is None:
        # PUBLISH BEFORE THE CHILD RUNS, the same producer-authoritative rule the
        # native leg follows. A PROJECT seat's family is not in its name — it is
        # in this record (`registered_seat_family`), and the child's SessionStart
        # resolves the family before it can bind anything. Publishing after the
        # launch meant the first hook of every project-named headless seat
        # resolved nothing, returned False, and the spawn then wrote session None
        # with no backfill on this leg at all. The pid is not knowable yet, so it
        # is published as None and written by the finalize below — under the ONE
        # lock this leg never releases, because a detached launch is a Popen that
        # returns immediately and waits on no hook, so there is no window for the
        # child's hook to observe a record without its pid.
        rec.update(harness="headless", pid=None, pid_identity=None)
        attempt = _publish_spawn_attempt(seat_name, identity, d, rec)
        if attempt is None:
            _restore_spawn_register(d, prior_rec)
            print("helm seat: %s could not be registered, so NOTHING was "
                  "launched — a headless seat whose register does not exist "
                  "cannot resolve its own family at SessionStart or be found "
                  "for the next duplicate reap" % identity, file=sys.stderr)
            return _unwound()
        try:
            pid = _headless_spawn(launch_sh, onboard, cwd,
                                  os.path.join(d, "spawn.log"), role=role,
                                  token=attempt["id"])
        except OSError as e:
            # A LAUNCH THAT RAISED STARTED NOTHING: the process is absent by
            # construction, so the seat goes back exactly as it was.
            _restore_spawn_register(d, prior_rec)
            print("helm seat: headless spawn failed: %s" % e, file=sys.stderr)
            return _unwound()
        rec.update(harness="headless", pid=pid,
                   pid_identity=_pid_identity(pid))
        if not _finalize_spawn_attempt(seat_name, identity, d, rec, attempt):
            if rec["pid_identity"] is None:
                print("helm seat: WARNING — unregistered headless pid %d has no "
                      "birth identity; refusing an unverified cleanup signal"
                      % pid, file=sys.stderr)
                # DELIBERATELY unsignalled, so it is running — the strongest
                # survivor there is. It used to leave the re-minted script by
                # falling out of the rollback silently; the same outcome is now
                # the LAW being applied, and said out loud. A process helm
                # refuses to signal is the opposite of a proven absence, so the
                # record is KEPT as an incomplete attempt naming it — WITH the
                # pid, which is the whole of what it knows about the survivor.
                settled = _settle_spawn_attempt(
                    d, prior_rec, attempt, False,
                    "headless pid %d was left running (no birth identity, so "
                    "helm refused to signal it)" % pid,
                    retained={"pid": pid, "pid_identity": rec["pid_identity"]})
                print("helm seat: " + _settled_where_claim(identity, settled),
                      file=sys.stderr)
                return _unwound(
                    "headless pid %d was left running (no birth identity, so "
                    "helm refused to signal it)" % pid)
            survivor = None
            try:
                from . import seat_exit_owner
                _reason, unavailable = seat_exit_owner.terminate_process(
                    pid, lambda: _recorded_pid_alive(rec))
                if unavailable:
                    print("helm seat: WARNING — unregistered headless pid %d "
                          "cleanup was not proven: %s" % (pid, unavailable),
                          file=sys.stderr)
                    survivor = "headless pid %d was not proven terminated (%s)" \
                        % (pid, unavailable)
            except OSError as e:
                print("helm seat: WARNING — unregistered headless pid %d could "
                      "not be stopped: %s" % (pid, e), file=sys.stderr)
                survivor = "headless pid %d could not be stopped (%s)" % (pid, e)
            # `survivor` IS the absence measurement, already taken: None means
            # `terminate_process` proved this pid gone, anything else names why it
            # could not. One reading, both decisions — the rollback of the launch
            # script and the fate of the register — and the pid rides into the
            # kept record, never None over the value this leg just produced.
            settled = _settle_spawn_attempt(
                d, prior_rec, attempt, survivor is None, survivor or "",
                retained={"pid": pid, "pid_identity": rec["pid_identity"]})
            if survivor is not None:
                print("helm seat: " + _settled_where_claim(identity, settled),
                      file=sys.stderr)
            return _unwound(survivor)
        _ensure_autocompact_timer()
        print("helm seat: spawned %s HEADLESS (pid %d, detached; log %s) — "
              "onboarding rides as its first prompt (beacon-arm + @%s work); "
              "`helm seat where %s` resolves it"
              % (identity, pid, os.path.join(d, "spawn.log"), identity,
                 seat_name))
        return 0
    # PUBLISH BEFORE THE PANE COMMAND RUNS — the producer-authoritative rule the
    # native leg already follows, now on this leg too. A PROJECT seat's family is
    # not in its name; it is in this record (`registered_seat_family`), and the
    # child's first SessionStart resolves the family BEFORE it can bind anything.
    # Publishing after `ad.spawn` meant the first hook of every project-named
    # proxy seat resolved nothing, the binder returned False, and the spawn then
    # wrote session None over it — with a backfill only Orca has. The handle is
    # not knowable yet, so it is published as None and written by the finalize;
    # the binder lets a hook bind into a PENDING attempt's absent handle ONLY
    # when that hook carries this attempt's token, which the launch command
    # below threads into the child — so the rendezvous is with THIS spawn's
    # child and not with any same-seat pane that happens to be starting.
    rec.update(harness=ad.name, handle=None)
    attempt = _publish_spawn_attempt(seat_name, identity, d, rec)
    if attempt is None:
        _restore_spawn_register(d, prior_rec)
        print("helm seat: %s could not be registered, so NOTHING was spawned — "
              "a pane whose register does not exist cannot resolve its own "
              "family at SessionStart or be found for the next duplicate reap"
              % identity, file=sys.stderr)
        return _unwound()
    handle = None
    spawn_failure = None
    onboard_state = onboard_proof = None
    # THE LOCK COMES OFF FOR THE CHILD, and only for the child. The adapter
    # create, the pane-boot grace and the onboarding submit all wait on the pane,
    # and the pane's own SessionStart runs inside that window under a 5s hook
    # budget, taking this very lock to bind its session and initialize its room
    # cursors. Holding it across the send delay could get the child's own hook
    # killed mid-bind. Serialization is not dropped: the PENDING attempt above is
    # what keeps another spawn or resume out while this window is open.
    with _seat_lifecycle_impl._seat_lifecycle_lock_released(d):
        try:
            handle = ad.spawn(
                _seat_lifecycle_impl._launch_command(
                    launch_sh, role, token=attempt["id"]),
                title=identity, cwd=cwd)
            # SUBMIT through the SHARED rung: the short exact-visible bootstrap
            # prints the long onboarding brief from this new process's own
            # seat/room/role environment. Sending the full wrapped brief would
            # force prefix authorization, where a human suffix is
            # indistinguishable from terminal wrapping. `_spawn_submit` owns the
            # tri-state (and the NOT_DELIVERED raise the cleanup below catches)
            # for both families.
            onboard_state, onboard_proof = _spawn_submit(ad, handle, seat_name)
        except harness.HarnessError as e:
            spawn_failure = e
    if spawn_failure is not None:
        e = spawn_failure
        cleanup = ""
        survivor = None
        if handle:
            try:
                from . import seat_exit_owner
                _reason, unavailable = seat_exit_owner.stop_pane(ad, handle)
                cleanup = ("; incomplete pane %s closed" % handle) \
                    if unavailable is None else \
                    ("; WARNING incomplete pane %s close unproven: %s"
                     % (handle, unavailable))
                if unavailable:
                    # The pane may STILL BE RUNNING, on the script minted a few
                    # lines up. helm already measures this — it is the same
                    # `unavailable` the warning above is built from — and the
                    # rollback used to ignore it and restore anyway.
                    survivor = "pane %s could not be proven closed (%s)" \
                        % (handle, unavailable)
            except Exception as stop_err:
                cleanup = "; WARNING incomplete pane %s not closed: %s" \
                    % (handle, stop_err)
                survivor = "pane %s could not be closed (%s)" \
                    % (handle, stop_err)
        if handle is None and not getattr(
                ad, "spawn_failure_absent", lambda _e: False)(e):
            survivor = ("the adapter may have created a remote pane before its "
                        "create reply failed; no handle exists to prove it absent")
        print("helm seat: %s spawn via %s failed: %s%s"
              % (seat_name, ad.name, e, cleanup), file=sys.stderr)
        # `survivor` IS the absence measurement this branch already took, in all
        # three of its shapes (a proven pane close, an unproven one, and a create
        # whose own adapter cannot say whether a remote pane exists). One reading
        # decides both the launch-script rollback and the register's fate, so the
        # two can never disagree about whether a pane survived — and the handle,
        # where one was returned, rides into the kept record.
        settled = _settle_spawn_attempt(
            d, prior_rec, attempt, survivor is None, survivor or "",
            retained={"handle": handle})
        if survivor is not None:
            print("helm seat: " + _settled_where_claim(identity, settled),
                  file=sys.stderr)
        return _unwound(survivor)
    final = _finalize_spawn_attempt(seat_name, identity, d, rec, attempt,
                                   handle=handle)
    if final is None:
        survivor = None
        try:
            from . import seat_exit_owner
            _reason, unavailable = seat_exit_owner.stop_pane(ad, handle)
            if unavailable:
                print("helm seat: WARNING — unregistered pane %s close was not "
                      "proven: %s" % (handle, unavailable), file=sys.stderr)
                survivor = "pane %s could not be proven closed (%s)" \
                    % (handle, unavailable)
        except Exception as e:
            print("helm seat: WARNING — unregistered pane %s could not be "
                  "closed: %s" % (handle, e), file=sys.stderr)
            survivor = "pane %s could not be closed (%s)" % (handle, e)
        settled = _settle_spawn_attempt(
            d, prior_rec, attempt, survivor is None, survivor or "",
            retained={"handle": handle})
        print("helm seat: %s could not record pane %s — %s"
              % (identity, handle, _settled_where_claim(identity, settled)),
              file=sys.stderr)
        return _unwound(survivor)
    rec = final
    if isinstance(ad, harness.OrcaAdapter) and not \
            _backfill_spawn_session(seat_name, d, ad):
        print("helm seat: WARN — spawned pane session identity is not yet "
              "proven; SessionStart must bind it before autocompact can act",
              file=sys.stderr)
    _ensure_autocompact_timer()
    # THE OUTCOME BECOMES STATE BEFORE IT BECOMES A SENTENCE. Recorded on
    # BOTH branches: a record with no `onboarding` field predates this and
    # reads ABSENT at the door, never "proven".
    _record_onboarding(d, seat_name, onboard_state, onboard_proof)
    # "onboarding SENT" was the overclaim: it named the transport, not the
    # turn. Only DELIVERED earns the success sentence.
    if onboard_state == harness.DELIVERED:
        print("helm seat: spawned %s via %s — pane %s; onboarding submitted "
              "(beacon-arm + @%s work); `helm seat where %s` resolves it"
              % (identity, ad.name, handle, identity, seat_name))
        return 0
    # UNKNOWN: the pane is up AND REGISTERED — say so, so nobody re-spawns a
    # duplicate — but the brief is unproven, and a spawn that cannot prove its
    # seat was briefed has not finished. rc 1, loudly, in the same spirit as
    # the wake-path leg one function up.
    print("helm seat: spawn of %s via %s INCOMPLETE — pane %s is up and "
          "REGISTERED (do not re-spawn), but its onboarding brief is NOT "
          "PROVEN submitted (%s: %s). Read the pane: if the brief is sitting "
          "unsent in the composer, submit it; the seat is otherwise blank."
          % (identity, ad.name, handle, onboard_state, onboard_proof),
          file=sys.stderr)
    return 1


def _panes(rest):
    """seat panes — every metaharness pane, GROUPED BY PROVENANCE.

    Deliberately not one flat list. helm-spawned and orca-adopted seats support
    different verbs (only the first has a launch.sh, a proxy and a spawn
    register), and an operator who cannot tell them apart reaches for a verb
    that silently does not apply — which is the class of confusion that
    produced a false "already landed" claim. Grouping IS the safety property.
    """
    from . import orcaadopt, seats
    procs, unreadable = orcaadopt.claude_processes()
    rows, note = orcaadopt.pane_rows(procs=procs, unreadable=unreadable)
    if "--json" in rest:
        print(json.dumps({"panes": rows, "note": note,
                          "unidentified_claude_pids": unreadable}, indent=2,
                         sort_keys=True))
        return 0
    if note:
        print("helm seat: no pane inventory — " + note, file=sys.stderr)
        return 1
    groups = [(orcaadopt.HELM_SPAWNED, "helm spawned these — full seat verbs "
               "(launch.sh, proxy, spawn register)"),
              (orcaadopt.ORCA_ADOPTED, "the metaharness launched these — "
               "resume replays the TRANSCRIPT; no launch.sh"),
              (orcaadopt.UNOWNED, "no helm seat identity found (not a helm "
               "seat, or it never announced one)")]
    for prov, blurb in groups:
        rows_in = [r for r in rows if r.get("provenance") == prov]
        print("%s (%d) — %s" % (prov, len(rows_in), blurb))
        for r in sorted(rows_in, key=lambda r: (r.get("seat") or "~",
                                                r.get("handle") or "")):
            # LAUNDER THE NAME AT THE SINK. Two unvalidated sources reach this
            # column and neither is checked at its join seam: a pane's
            # HELM_CHAT_NAME, and — as of the session join — a roster KEY. A
            # hostile name lands in either verbatim, and this is the first and
            # widest column of the listing, so an ESC/bidi payload reshapes the
            # terminal of the operator reading it. Same scrub every other
            # roster-borne display string clears; `fleet.py` printed a raw key
            # into exactly this shape of column until 2026-08-04.
            print("    %-18s %-14s %-42s %s"
                  % (seats._seat_label(r.get("seat")) if r.get("seat") else "-",
                     r.get("status") or "?",
                     (r.get("worktree") or "-")[:42], r.get("handle")))
    print("%d pane%s total" % (len(rows), "" if len(rows) == 1 else "s"))
    if unreadable:
        # An `unowned` row is only honestly "no identity found" when the lookup
        # could see everything. It could not, so say so rather than let the
        # label overclaim.
        print("  NOTE: %d live claude process%s could not be identified "
              "(environ unreadable: pid %s) — an 'unowned' row above may in "
              "fact belong to one of them"
              % (len(unreadable), "" if len(unreadable) == 1 else "es",
                 ", ".join(str(p) for p in unreadable)))
    # THE SAME LAW, THE OTHER BLINDNESS. A seat that never exported
    # HELM_CHAT_NAME is identified by the SESSION join, and that join reads the
    # chat roster. With the roster unreadable those rows fall to `unowned` for a
    # reason that is "could not tell", not "no identity found" — and this is the
    # surface an operator reads before concluding a live seat is unreachable.
    partial = sorted({r["identity_partial"] for r in rows
                      if r.get("identity_partial")})
    for reason in partial:
        print("  NOTE: %s — an 'unowned' row above may be a seat this listing "
              "could not name" % reason)
    return 0


def _boot_brief_wire(rearm=False):
    """One exactly visible turn that lets the seat print its full long brief."""
    return "Run `helm seat boot-brief%s` and follow it." % (
        " --rearm" if rearm else "")


def _boot_brief(rest):
    """Print this process's deterministic onboarding or restart brief."""
    from . import seats
    seat_name = seats.own_name()
    if not seat_name:
        print("helm seat: boot-brief requires this process's HELM_CHAT_NAME",
              file=sys.stderr)
        return 1
    from .seat_role import SEAT_ROLES
    role = os.environ.get("HELM_SEAT_ROLE") or "worker"
    if role not in SEAT_ROLES:
        print("helm seat: boot-brief found invalid HELM_SEAT_ROLE=%s" % role,
              file=sys.stderr)
        return 1
    if "--rearm" in rest:
        print(rearm_prompt(seat_name, role=role))
    else:
        print(onboarding_prompt(
            seat_name, os.environ.get("HELM_CHAT_ROOM") or None, role=role))
    return 0


def cmd_seat(args):
    """seat add|up|down|launch|spawn|where|rebind|resume|retitle|smoke|list|
    status|doctor|lifecycle — multimodel seats."""
    args = list(args)
    if not args:
        print(_USAGE, file=sys.stderr)
        return 2
    verb, rest = args[0], args[1:]
    from .cli import guard_tail
    if verb in ("list", "status"):
        rc = guard_tail("helm seat " + verb, rest, usage=_USAGE)
        if rc is not None:
            return rc
        return _status(rest)
    if verb == "doctor":
        rc = guard_tail("helm seat doctor", rest,
                        flags=("--ensure", "--json", "--quiet"),
                        usage=_USAGE)
        if rc is not None:
            return rc
        if "--json" in rest and "--ensure" not in rest:
            print("helm seat: --json rides doctor --ensure (the machine-read "
                  "surface); plain doctor is the human one", file=sys.stderr)
            return 2
        return _ensure(rest) if "--ensure" in rest else _doctor(rest)
    if verb == "cred-follow":
        # The owner's question ("why do I have to switch it by hand after I
        # already switched in orca?") gets an operator door of its own as well
        # as the two passes it rides, because a dry run IS the answer: the
        # table says whether the pool follows orca right now.
        rc = guard_tail("helm seat cred-follow", rest,
                        flags=("--apply", "--json"), usage=_USAGE)
        if rc is not None:
            return rc
        from . import codexhomes
        return codexhomes.cmd_cred_follow(rest)
    if verb == "lifecycle":
        from . import seat_ledger
        return seat_ledger.cmd_lifecycle(rest)
    if verb == "autocompact":
        from . import autocompact
        return autocompact.cmd_autocompact(rest)
    if verb == "resume-turn":
        from . import resumeturn
        return resumeturn.cmd_resume_turn(rest)
    if verb == "boot-brief":
        rc = guard_tail("helm seat boot-brief", rest, flags=("--rearm",),
                        usage="seat boot-brief [--rearm]")
        return rc if rc is not None else _boot_brief(rest)
    if verb == "silent-drop":
        from . import silent_drop
        rc = silent_drop.cmd_silent_drop(rest)
        # task/1039: the rogue-compute watchdog RIDES this 90s cadence (its
        # timer already exists; a new daemon would be the built-not-wired
        # class). Guarded both ways: a rogue-pass failure never blocks drop
        # detection, and the drop rc above is preserved untouched.
        try:
            from . import roguescan
            roguescan.cadence_pass(dry="--dry-run" in rest)
        except Exception as e:
            print("helm seat silent-drop: rogue-compute pass failed: %s" % e,
                  file=sys.stderr)
        return rc
    if verb == "idle-dispatch":
        from . import idle_dispatch
        return idle_dispatch.cmd_idle_dispatch(rest)
    if verb == "reassign":
        # THE ONE DOOR for a dead or renamed seat's holdings (the dead-seat holdings mandate).
        # Deliberately NOT folded into `rebind`: rebind moves ONE obligation
        # and its evidence gate admits starvation or context exhaustion, and a
        # dead seat is neither. See helm/seat_reassign.py for why all three
        # existing movers refuse this population.
        from . import seat_reassign
        return seat_reassign.cmd_reassign(rest)
    if verb == "unblock":
        from . import planprompt
        return planprompt.cmd_unblock(rest)
    if verb == "spawn":
        if not rest:
            print("usage: helm seat spawn <seat> [--room R] [--cwd DIR] "
                  "[--model M] [--role worker|lead] [--replace] [--print]",
                  file=sys.stderr)
            return 2
        return _spawn(rest[0], rest[1:])
    if verb == "rehome":
        # THE ONE-VERB FORM of a hand procedure the integrator ran twice in a
        # week (task/2573): exit a live seat cleanly and bring it back on a
        # named credhome with the same session. It owns its own tail guard and
        # is dry-run without --apply; its whole point is the REFUSAL that the
        # hand procedure had no room for — a home whose refresh chain has
        # expired is not synced from Orca, and `helm launch` starts on the
        # stale token rather than declining.
        from . import seat_rehome
        return seat_rehome.cmd_rehome(rest)
    if verb == "where":
        if not rest:
            print("usage: helm seat where <seat> [--json]", file=sys.stderr)
            return 2
        return _where(rest[0], rest[1:])
    if verb == "rebind":
        # GUARD THE TAIL, NOT THE SEAT NAME. rebind takes `<seat>` OR `--all`,
        # so handing the positional to a flags-only guard rejected the entire
        # single-seat form — `helm seat rebind gemini` answered "unknown arg
        # 'gemini'" while printing a usage line that shows exactly that call.
        # Only --all ever worked, which is why the fleet-wide reboot repair
        # passed and this did not. `adopt` above already strips the positional
        # the same way; the tests called _rebind() directly and so ran under
        # the dispatcher rather than through it.
        # Guard the FLAGS only. Stripping just a LEADING positional was still
        # wrong: `rebind --all gemini` put the name back in front of the guard,
        # which rejected it with a generic "unknown arg" and pre-empted
        # _rebind's precise "--all takes no seat name". guard_tail exists to
        # catch a typo'd flag; positionals are _rebind's to validate, and it
        # already refuses both the --all-plus-name and the empty case.
        rc = guard_tail("helm seat rebind",
                        [a for a in rest if a.startswith("--")],
                        flags=("--all", "--apply", "--install-timer"),
                        usage="seat rebind <seat>|--all [--apply] [--install-timer]")
        if rc is not None:
            return rc
        return _rebind(rest)
    if verb == "panes":
        rc = guard_tail("helm seat panes", rest, flags=("--json",),
                        usage="seat panes [--json]")
        if rc is not None:
            return rc
        return _panes(rest)
    if verb == "retire-deny":
        # The DELIBERATE door for a deny entry no record owns: a refresh
        # leaves such an entry alone and names this verb (seat_catalog
        # RETIRED_SPAWN_DENIES). Dry-run default; it guards its own tail.
        from . import seat_launch_assets as _assets
        return _assets.cmd_retire_deny(rest)
    if verb == "retitle":
        # THE TITLE half of the post-reboot repair, on demand. The sweep
        # re-stamps titles after the boot that reverts them; this is the same
        # act without waiting for one, and its dry-run default is the owner's
        # checksum table. It guards its own tail.
        from . import orcatitle
        return orcatitle.cmd_retitle(rest)
    if verb == "composers":
        rc = guard_tail("helm seat composers", rest, flags=("--json",),
                        valued=("--submit",),
                        usage="seat composers [--json] [--submit HANDLE]")
        if rc is not None:
            return rc
        from . import composers
        return composers.cmd_composers(rest)
    if verb == "adopt":
        if not rest:
            print("usage: helm seat adopt <seat> [--repo DIR] [--base REF]",
                  file=sys.stderr)
            return 2
        rc = guard_tail("helm seat adopt", rest[1:], valued=("--repo", "--base"),
                        usage="seat adopt <seat> [--repo DIR] [--base REF]")
        if rc is not None:
            return rc
        return _adopt(rest[0], rest[1:])
    if verb == "resume":
        if "--all" in rest:
            # THE POST-REBOOT SWEEP: every registered seat classified through
            # rebind's proof, the dead ones relaunched. Routed before the
            # single-seat guard because `--all` is a sweep, not a seat name;
            # the verb guards its own tail.
            from . import seat_resume_all
            return seat_resume_all.cmd_resume_all(rest)
        if not rest:
            print("usage: helm seat resume <seat>|--all [--cwd DIR] "
                  "[--session ID] [--role worker|lead] [--force] [--apply]",
                  file=sys.stderr)
            return 2
        # resume RELAUNCHES the pane — trailing junk refuses before it fires.
        # --force is admitted because an orca-adopted resume is REFUSED by the
        # duplicate-session guard on LIVE/UNKNOWN, and an operator who has
        # personally confirmed a zombie needs a way to say so. --cwd overrides
        # the recorded/sniffed cwd (row #155 — the shared checkout is the sane
        # value when the operator wants every pane findable in one tree).
        # --session pins the exact transcript (the rescue path: cv prune
        # prints the new id, resume must attach THAT copy — the walled
        # original otherwise wins the content race 3 times out of 5).
        rc = guard_tail("helm seat resume", rest[1:], flags=("--force",),
                        valued=("--cwd", "--session", "--role"),
                        usage="seat resume <seat> [--cwd DIR] [--session ID] "
                              "[--role worker|lead] [--force]")
        if rc is not None:
            return rc
        return _resume(rest[0], rest[1:])
    if verb in ("add", "up", "down", "launch", "smoke"):
        if not rest:
            print("usage: helm seat %s <family>" % verb, file=sys.stderr)
            return 2
        family = rest[0]
        # per-verb tail contract — refuse trailing junk BEFORE anything runs:
        # `seat down codex --bogus --help` used to STOP the seat and exit 0.
        tails = {"add": ((), ("--room", "--auth-from", "--key-from",
                              "--provider")),
                 "up": ((), ()),
                 "down": ((), ()),
                 "smoke": (("--multi",), ()),
                 "launch": (("--multi",),
                            ("--room", "--model", "-i", "--instance"))}
        tflags, tvalued = tails[verb]
        rc = guard_tail("helm seat " + verb, rest[1:], flags=tflags,
                        valued=tvalued, usage=_USAGE)
        if rc is not None:
            return rc
        # --multi (launch/smoke): the mixed-model fleet shape — no subagent
        # pin, probe agents minted, smoke grows the fan-out leg.
        multi = "--multi" in rest
        # add/launch are self-contained presets: explicit --room wins, then
        # inherited launch homing, then the current git project's default.
        explicit_room = None
        if "--room" in rest:
            try:
                explicit_room = rest[rest.index("--room") + 1]
            except IndexError:
                print("helm seat: --room wants a value", file=sys.stderr)
                return 2
        room, room_source = (None, None)
        if verb in ("add", "launch"):
            room, room_source = _resolve_homing(explicit_room)
        if verb == "add":
            return _add(
                family, rest[1:], room=room, room_source=room_source)
        if verb in ("up", "down"):
            # `helm seat up codex-3` targets instance codex-3's OWN proxy;
            # `helm seat up codex` targets the family (instance-1) proxy.
            fam_name, seat_name = _split_seat(family)
            # A KNOWN NATIVE SEAT GETS A PLAIN REASON, not "unknown family
            # 'claude'". These two verbs are proxy verbs: `_up`/`_down` call
            # `_require_seat`, which indexes the proxy table, so a registered
            # native seat — whose own register names family claude, and whose
            # project round-trip the synopsis advertises into these verbs —
            # arrived here and was told its family does not exist. It does; it
            # has no proxy. Asked of the REGISTER, so a bare `claude` (which is
            # not a seat at all) still falls through to the unknown-family door.
            if registered_seat_family(seat_name)[0] == NATIVE_FAMILY:
                print("helm seat: %s is a registered NATIVE claude seat — it "
                      "runs no proxy, so `seat %s` has nothing to start or "
                      "stop. Its pane IS its runtime: `helm seat where %s` "
                      "reads it and `helm seat spawn %s --replace` relaunches "
                      "it." % (seat_name, verb, seat_name, seat_name),
                      file=sys.stderr)
                return 2
            fn = _up if verb == "up" else _down
            return fn(fam_name, seat=seat_name)
        if verb == "smoke":
            return _smoke(family, multi=multi)
        fam = _require_seat(family)
        if fam is None:
            return 1
        model = rest[rest.index("--model") + 1] if "--model" in rest else None
        # A proxy family's --model must never be one of the built-in SUBAGENT
        # FRONTMATTER ids. The proxy's models block aliases those ids to the
        # family model (task/1952) as a SUBAGENT convenience, never a seat
        # runtime: a seat launched on one would route and could never ATTEST
        # (proxywatch binds seat proofs against the family's catalogued
        # routes only — a finding the integrator ruled on). Refuse it
        # HERE, at the launch door, naming why. This is a refusal of exactly
        # the alias ids, NOT a whitelist: other legitimate alternates a
        # family serves (gpt-5.5 on codex, spark) keep working.
        # slice 6 — N instances of one family share the OAuth cred POOL but each
        # gets its own proxy fate: -i/--instance N -> seat codex-N on its own
        # port (default 1 = the family proxy, today's exact line).
        inst = 1
        for flag in ("-i", "--instance"):
            if flag in rest:
                try:
                    inst = int(rest[rest.index(flag) + 1])
                except (ValueError, IndexError):
                    print("helm seat: %s wants an integer" % flag, file=sys.stderr)
                    return 2
        seat = family if inst <= 1 else "%s-%d" % (family, inst)
        # no explicit --model -> the seat's PERSISTED choice (spawn.json), so
        # the refresh below cannot rewrite a spark seat's launch.sh (and the
        # printed line) back to the family default (land af391eab). An
        # explicit --model still wins for this mint.
        model = model or _persisted_model(_instance_dir(family, seat), seat)
        from .seat_catalog import proxy_runtime_model_error
        model_error = proxy_runtime_model_error(family, model)
        if model_error:
            print("helm seat: " + model_error, file=sys.stderr)
            return 2
        ownership = _seat_surface_error(family, seat)
        if ownership:
            print("helm seat: " + ownership, file=sys.stderr)
            return 1
        # Per-instance proxies are a PROXY-family (OAuth-pool) feature only.
        # proxy-key families (kimi) bake ONE key into the family config — there
        # is no pool to point an instance config at, so `_mint_instance_proxy`
        # skips config generation and `up <instance>` must refuse. Accepting
        # `launch kimi -i 2` would print an instance line whose proxy can never
        # come up (and its derived port can collide with a sibling family's
        # block). Refuse up front: this is an unsupported-family gate, not a
        # warn — the launch cannot produce a working instance.
        if inst > 1 and fam["mode"] != "proxy":
            print("helm seat: per-instance proxies need an OAuth-pool family "
                  "(mode=proxy); %s is mode=%s — only `helm seat launch %s` "
                  "(instance 1) is supported"
                  % (family, fam["mode"], family), file=sys.stderr)
            return 2
        # AN INSTANCE THAT CANNOT HAVE ITS OWN ENDPOINT IS NOT LAUNCHABLE, and
        # this verb is the second door that mints assets carrying one. It had no
        # endpoint admission, so `-i 183` — whose derived port lands in the
        # allocated project block — reached the renderer, which has nothing to
        # render. Asked of the PURE predicate: this door previews and re-mints,
        # and an allocating gate would spend a durable slot on every call.
        endpoint = _instance_endpoint_error(family, seat)
        if endpoint:
            print("helm seat: " + endpoint, file=sys.stderr)
            return 2
        # guards ride stderr (stdout stays the bare pasteable line), warn
        # never refuse: over-capacity burns one pool faster (fall-through
        # masks it) and a live same-named seat is a relaunch-vs-collision the
        # operator calls (a crashed seat must not brick its slot).
        if inst > 1:
            from . import codexhomes, seats as _seats
            cap = codexhomes.capacity()
            # A POOL NOBODY COULD READ IS UNKNOWN CAPACITY, AND A VALID LAUNCH
            # STILL GOES THROUGH (task/2480 R5). `total` is 0 for an unread
            # pool, which would render as "instance 2 exceeds pooled fleet
            # capacity 0" — a measured over-capacity claim produced by a
            # directory fault. And the opposite failure is just as bad: this
            # rung has ALWAYS been warn-never-refuse, so a raise from the pool
            # reader aborted a launch whose family, model, ownership and
            # instance were all valid. Say UNKNOWN, and carry on.
            if cap.get("unknown"):
                print("helm seat: WARN — pooled fleet capacity is UNKNOWN "
                      "(%s); launching anyway, the capacity rung advises and "
                      "never refuses" % cap["error"], file=sys.stderr)
            elif inst > cap["total"]:
                print("helm seat: WARN — instance %d exceeds pooled fleet "
                      "capacity %d (`helm codex capacity`); the pool falls "
                      "through usage caps but %d concurrent seats burn it "
                      "faster" % (inst, cap["total"], inst), file=sys.stderr)
            # THE ROSTER'S OWN SPELLING. A case variant read as no row, so
            # the collision WARN below — the one line telling an operator a
            # seat is already live before they relaunch over it — stayed
            # silent for exactly the spelling a human is most likely to type.
            row = _seats.seat_row(seat)[0]
            ls = _seats.last_seen(seat, row) if row else None
            if row and ls and time.time() - ls < _seats.QUIET_S:
                print("helm seat: WARN — seat %r already live on the roster "
                      "(last seen %.0fs ago) — relaunch or collision is your "
                      "call" % (seat, time.time() - ls), file=sys.stderr)
        # launch REFRESHES the assets first (G-seatlaunch-installs): delivery
        # hooks + beacon permit + a launch.sh carrying the CURRENT identity
        # shape — retrofitting a seat minted before either existed. stdout
        # stays exactly the pasteable line; notes ride stderr. The seat directory
        # is infrastructure and may keep its old name after a roster rename, so
        # resolve the launch identity ONCE and feed the same answer to both the
        # generated asset and the printed command.
        from .seat_launch_assets import _launch_identity
        identity, identity_error = _launch_identity(seat)
        if identity_error:
            print("helm seat: REFUSED — launch identity for %s is unresolved: %s; "
                  "no launch asset was written and no session was started"
                  % (seat, identity_error), file=sys.stderr)
            return 1
        launch_dir = _instance_dir(family, seat)
        with _seat_lifecycle_lock(launch_dir):
            refused = _write_launch_assets(
                family, launch_dir, room, seat, room_source=room_source,
                multi=multi, model=model, identity=identity) \
                is _SEAT_SURFACE_REFUSED
        if refused:
            return 1
        if seat != family:
            # per-instance proxies: this instance gets its OWN port/config/
            # token/log, so one instance's restart/429-stall never takes a
            # sibling down (the shared-8317 blast-radius). OAuth pool stays
            # family-level — no quota multiplication.
            _mint_instance_proxy(family, seat)
            print("helm seat: %s gets its own proxy — `helm seat up %s` "
                  "(127.0.0.1:%d) before launching"
                  % (seat, seat, _launch_endpoint(family, seat)),
                  file=sys.stderr)
        _ensure_autocompact_timer()
        # the pasteable line: export the bearer from its 0600 file (builtin, no
        # argv), then the env/claude command — the token never transits argv.
        print(_token_export(family, seat) + launch_line(
            family, model, room, seat, room_source=room_source, multi=multi,
            identity=identity))
        from . import hooks
        hooks.surface_uncovered(out=sys.stderr)  # a running joined-late pane
        return 0                                 # still needs its relaunch
    print("helm seat: unknown verb '%s'" % verb, file=sys.stderr)
    print(_USAGE, file=sys.stderr)
    return 2

from . import seat_compat as _seat_compat
globals().update(_seat_compat.EXPORTS)
_SEAT_IMPL_MODULES = _seat_compat.IMPL_MODULES
del _seat_compat
assert not _unbacked_window_reason(), _unbacked_window_reason()
assert not _family_owner_aliases_are_unique(), _family_owner_aliases_are_unique()
assert not _project_port_block_is_clear(), _project_port_block_is_clear()
assert not _numbered_port_reservations_are_disjoint(), \
    _numbered_port_reservations_are_disjoint()


def _seed_impl_modules():
    namespace = {name: value for name, value in globals().items() if not (name.startswith("__") and name.endswith("__"))}
    for module in _SEAT_IMPL_MODULES:
        module.__dict__.update(namespace)
    return frozenset(namespace)


class _SeatModule(sys.modules[__name__].__class__):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name in self._SEAT_FANOUT_NAMES:
            for module in self._SEAT_IMPL_MODULES:
                setattr(module, name, value)
    def __delattr__(self, name):
        modules = tuple(self._SEAT_IMPL_MODULES)
        super().__delattr__(name)
        for module in modules:
            if name in module.__dict__:
                delattr(module, name)

_SEAT_FANOUT_NAMES = _seed_impl_modules()
sys.modules[__name__].__class__ = _SeatModule
