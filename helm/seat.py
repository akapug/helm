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

FLEET DELIVERY + IDENTITY: a seat is a first-class chat member. launch_line
exports HELM_CHAT_NAME=<family>, so the SessionStart join hook registers the
seat in the roster under its family name ('codex'/'kimi'/…) — @codex / @kimi
and owner posts then deliver to it between tool calls. The delivery hooks
(deliver + join) live in the seat's claude/ config dir; `helm hooks install`
wires them there (hooks.py's DELIVERY_SPECS) and `helm hooks status` reports
seat coverage. The same launch line wires dregg-native client signing:
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


def _resume(seat_name, rest, _locked=False, target_sid=None,
            expected_session=None, adapter=None):
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
    a REMOVED worktree, REFUSES loudly instead of spawning somewhere stale."""
    rest = list(rest)
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
        rc, lines = orcaadopt.resume(seat_name, force="--force" in rest)
        for line in lines:
            print(line, file=sys.stderr if rc else sys.stdout)
        return rc
    # Same admissibility gate as `_spawn`, BEFORE anything is minted (the
    # fable MED: resume bypassed it, so `resume kimi-2`/`resume codex-1` minted
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
                           adapter=adapter)
    launch_sh = os.path.join(d, "launch.sh")
    if not os.path.exists(launch_sh):
        print("helm seat: no %s seat minted (%s missing) — `helm seat add %s` "
              "then `helm seat launch %s` first"
              % (seat_name, launch_sh, family, seat_name), file=sys.stderr)
        return 1
    room, room_source = _homing_from_launch(launch_sh)
    multi = _multi_from_launch(launch_sh)
    prior = _spawn_record(d) or {}
    prior_sid = prior.get("session") if prior.get("seat") == seat_name else None
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
                     else _newest_seat_session(d, prefer_source=prior_sid))
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
    if room is None:
        # launch.sh carries no explicit room stamp (minted room-less). Resume
        # must still preserve the seat's DERIVABLE home — re-minting with
        # room=None would stamp the relaunch HELM_CHAT_ROOM-less and the
        # SessionStart join would fall back to #main, silently dropping the
        # seat out of its project room (the kimi room-drop regression,
        # 2026-07-23). Fall back to the one precedence (explicit env >
        # cwd-derived project room); a project-less seat stays un-homed.
        from . import seats as _seats
        room, room_source = _seats.resolve_homing(cwd=resume_cwd)
    command = "%s %s" % (shlex.quote(launch_sh),
                         ("--resume " + shlex.quote(sid)) if sid else "--continue")
    from . import harness
    ad = adapter or harness.detect()
    if ad is None:
        print("helm seat: " + harness.RECOMMENDATION, file=sys.stderr)
        print("  manual paste (env refreshed, session kept): " + command,
              file=sys.stderr)
        return 1
    # Stop only the authoritative recorded process/pane BEFORE re-minting: its
    # `sh` is executing THIS launch.sh. Mutable titles are never identity; an
    # unregistered same-title pane blocks the resume instead of being destroyed.
    notes, errors = _reap_stale(
        seat_name, d, ad, allow_live=True, locked=True)
    for note in notes:
        print("  " + note)
    if errors:
        for error in errors:
            print("helm seat: " + error, file=sys.stderr)
        print("helm seat: resume aborted; resolve the unverified same-name pane "
              "before retrying", file=sys.stderr)
        return 1
    try:
        # env refresh half of the contract: the relaunch rides the LATEST
        # assets (identity vars, delivery hooks, context env), room preserved.
        if _write_launch_assets(
                family, d, room, seat_name,
                room_source=room_source, multi=multi) \
                is _SEAT_SURFACE_REFUSED:
            return 1
        from . import seats
        # resume must not strand the seat on a dead proxy either (the same
        # silent-dead-seat class the fable HIGH named in _spawn): mint the
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
        handle = ad.spawn(command, title=seat_name,   # safe_cwd: a deleted
                          cwd=resume_cwd or seats.safe_cwd())  # cwd must not
        # crash the resume (eager-getcwd class); spawn treats None as inherit.
    except harness.HarnessError as e:
        print("helm seat: %s resume via %s failed: %s"
              % (seat_name, ad.name, e), file=sys.stderr)
        return 1
    def stop_new():
        try:
            ad.stop(handle)
        except Exception as e:
            print("helm seat: WARNING — unregistered resumed pane %s could not "
                  "be closed: %s" % (handle, e), file=sys.stderr)

    if target_sid:
        live, live_err = _prove_spawned_pane(ad, handle)
        if not live:
            print("helm seat: resumed pane was not proven live: %s" % live_err,
                  file=sys.stderr)
            stop_new()
            return 1

    moved = []
    if prior_sid and sid and prior_sid != sid:
        try:
            moved = seats.rebind_claim_sessions(seat_name, prior_sid, sid)
        except Exception as e:
            print("helm seat: claim-session rebind failed before registration: %s"
                  % e, file=sys.stderr)
            stop_new()
            return 1

    from . import pk
    rec = {"v": 1, "seat": seat_name, "worktree": resume_cwd or os.getcwd(),
           "room": room or "main", "room_source": room_source,
           "launch_sh": launch_sh, "ts": pk.now_ts(), "harness": ad.name,
           "handle": handle, "session": sid}
    if not _register_spawn(seat_name, d, rec):
        try:
            rolled = seats.rollback_claim_sessions(
                seat_name, prior_sid, sid, moved)
            if rolled != len(moved):
                print("helm seat: WARNING — claim rollback restored %d/%d rows"
                      % (rolled, len(moved)), file=sys.stderr)
        except Exception as e:
            print("helm seat: WARNING — claim rollback failed: %s" % e,
                  file=sys.stderr)
        stop_new()
        return 1
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
    # this message describes, while `send` returns success.
    state, proof = ad.submit(handle, rearm_prompt(seat_name))
    if state != harness.DELIVERED:
        print("helm seat: resume of %s INCOMPLETE — pane %s is up but its "
              "wake path is NOT PROVEN re-armed (%s: %s). The seat may be "
              "ALIVE AND DEAF: no @mention, DM or brief can wake it until its "
              "beacon is re-armed. By hand, type into the pane: arm Monitor("
              "command: \"helm chat wait --seat %s --follow\", "
              "persistent: true)"
              % (seat_name, handle, state, proof, seat_name), file=sys.stderr)
        return 1
    print("helm seat: resumed %s via %s — pane %s, %s; env refreshed from %s; "
          "wake-path re-arm prompt sent (the beacon is per-session — the "
          "restart killed it)"
          % (seat_name, ad.name, handle,
             ("session %s… (--resume)" % sid[:8]) if sid
             else "--continue (newest session)", launch_sh))
    return 0


def _spawn(seat_name, rest, _locked=False):
    """seat spawn <seat> [--room R] [--cwd DIR] [--replace] [--print] — see
    comment above for the three paths + the common laws. Live replacements are
    serialized per seat so concurrent callers cannot both reap an empty slot,
    launch duplicates, and race the one spawn.json register."""
    family, err = _seat_family(seat_name)
    if err:
        print("helm seat: " + _unknown_seat_reason(seat_name, err),
              file=sys.stderr)
        return 2
    # Instance-spawn gate (the fable MED — it lived only on `launch`, so
    # `spawn kimi-2` / `spawn codex-1` minted launch lines pointed at a SIBLING
    # family's port range). ONE shared predicate with `_resume` (the second
    # fable MED: the gate on spawn alone let resume mint the refused seats).
    gate = _instance_gate(family, seat_name)
    if gate:
        print("helm seat: " + gate, file=sys.stderr)
        return 2
    d = _instance_dir(family, seat_name)
    launch_sh = os.path.join(d, "launch.sh")
    if not os.path.exists(launch_sh) and \
            not os.path.exists(os.path.join(seat_dir(family), "config.yaml")):
        print("helm seat: no %s seat minted — `helm seat add %s` first, then "
              "`helm seat spawn %s`" % (seat_name, family, seat_name),
              file=sys.stderr)
        return 1
    # provision=_locked: the home worktree is created exactly ONCE, inside the
    # per-seat spawn lock below (the unlocked pass exists only to surface an
    # argument error early, and the dry-run pass must stay side-effect free).
    parsed, arg_err = _spawn_args(rest, seat_name, provision=_locked)
    if arg_err:
        print("helm seat: %s; usage: helm seat spawn <seat> [--room R] "
              "[--cwd DIR] [--replace] [--print]" % arg_err, file=sys.stderr)
        return 2
    room, cwd, dry_run, replace = parsed
    # Room provenance rides with the room (roster-scatter class): an explicit
    # --room / launch.sh HELM_CHAT_ROOM is explicit; a launch.sh room the
    # seam stamped derived stays derived; NO room stays None — the roster
    # mirror must not invent a 'main' home the SessionStart join would never
    # have written (seats.resolve_homing derives the real one from cwd).
    room_source = "explicit" if room is not None else None
    if room is None:
        room, room_source = _homing_from_launch(launch_sh)
        room_source = room_source or ("explicit" if room else None)
    multi = _multi_from_launch(launch_sh)
    onboard = onboarding_prompt(seat_name, room)
    from . import harness
    ad = harness.detect()
    if dry_run:
        return _spawn_plan(seat_name, d, launch_sh, room, cwd, onboard, ad,
                           replace=replace)
    if not _locked:
        import fcntl
        os.makedirs(d, mode=0o700, exist_ok=True)
        with open(os.path.join(d, ".spawn.lock"), "a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            return _spawn(seat_name, rest, _locked=True)
    notes, reap_errors = _reap_stale(
        seat_name, d, ad, allow_live=replace, locked=True)
    for note in notes:
        print("  " + note)
    if reap_errors:
        for error in reap_errors:
            print("helm seat: " + error, file=sys.stderr)
        print("helm seat: replacement aborted; resolve the stale same-name seat "
              "before retrying", file=sys.stderr)
        return 1
    # mint hygiene AFTER the reap (a stale pane's sh may still be reading the
    # old launch.sh — the resume-verb ordering law), workdir=cwd so the trust
    # seed covers where the seat will actually run.
    # room_source rides INTO the re-minted script (HELM_CHAT_ROOM_SOURCE):
    # dropping it here laundered a derived room to explicit — the child's
    # SessionStart join then outranked (and overwrote) an operator-set home.
    if _write_launch_assets(family, d, room, seat_name, workdir=cwd,
                            room_source=room_source, multi=multi) \
            is _SEAT_SURFACE_REFUSED:
        return 1
    # per-instance proxy fate: an INSTANCE seat owns its OWN proxy
    # (instances/<seat>/), so spawn mints + starts THAT seat's proxy — never
    # the family's. MINT FIRST (the fable HIGH): a never-launched instance has
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
    rec = {"v": 1, "seat": seat_name, "worktree": cwd, "room": room,
           "room_source": room_source, "launch_sh": launch_sh,
           "ts": pk.now_ts(), "session": None}
    if ad is None:
        try:
            pid = _headless_spawn(launch_sh, onboard, cwd,
                                  os.path.join(d, "spawn.log"))
        except OSError as e:
            print("helm seat: headless spawn failed: %s" % e, file=sys.stderr)
            return 1
        rec.update(harness="headless", pid=pid,
                   pid_identity=_pid_identity(pid))
        if not _register_spawn(seat_name, d, rec):
            try:
                if rec["pid_identity"] is None or \
                        _recorded_pid_alive(rec) is True:
                    os.kill(pid, signal.SIGTERM)
            except OSError as e:
                print("helm seat: WARNING — unregistered headless pid %d could "
                      "not be stopped: %s" % (pid, e), file=sys.stderr)
            return 1
        _ensure_autocompact_timer()
        print("helm seat: spawned %s HEADLESS (pid %d, detached; log %s) — "
              "onboarding rides as its first prompt (beacon-arm + @%s work); "
              "`helm seat where %s` resolves it"
              % (seat_name, pid, os.path.join(d, "spawn.log"), seat_name,
                 seat_name))
        return 0
    handle = None
    try:
        handle = ad.spawn(shlex.quote(launch_sh), title=seat_name, cwd=cwd)
        try:
            delay = float(home.env("SPAWN_SEND_DELAY", SPAWN_SEND_DELAY_S))
        except (TypeError, ValueError):
            delay = SPAWN_SEND_DELAY_S
        if delay > 0:            # let claude reach its composer before the
            time.sleep(delay)    # onboarding keystrokes land
        # SUBMIT: an onboarding brief typed into the composer and never sent
        # produces a seat that boots, looks perfectly idle, and has never read
        # its own brief. The tri-state is preserved below rather than folded
        # into the spawn's success.
        onboard_state, onboard_proof = ad.submit(handle, onboard)
        if onboard_state == harness.NOT_DELIVERED:
            # PROVEN un-briefed. `submit` reports this instead of raising, so
            # without this rung the old cleanup below stopped firing and an
            # unbriefed pane started reporting rc 0 — the exact overclaim this
            # lane exists to delete, reintroduced by its own fix. A pane that
            # is seconds old and holds nothing but an unsubmitted brief is the
            # safe thing to close; UNKNOWN deliberately does NOT come here,
            # because destroying what we could not measure is worse.
            raise harness.HarnessError(
                "onboarding was not submitted — %s" % onboard_proof)
    except harness.HarnessError as e:
        cleanup = ""
        if handle:
            try:
                ad.stop(handle)
                cleanup = "; incomplete pane %s closed" % handle
            except harness.HarnessError as stop_err:
                cleanup = "; WARNING incomplete pane %s not closed: %s" \
                    % (handle, stop_err)
        print("helm seat: %s spawn via %s failed: %s%s"
              % (seat_name, ad.name, e, cleanup), file=sys.stderr)
        return 1
    rec.update(harness=ad.name, handle=handle)
    if not _register_spawn(seat_name, d, rec):
        try:
            ad.stop(handle)
        except harness.HarnessError as e:
            print("helm seat: WARNING — unregistered pane %s could not be "
                  "closed: %s" % (handle, e), file=sys.stderr)
        return 1
    if isinstance(ad, harness.OrcaAdapter) and not \
            _backfill_spawn_session(seat_name, d, ad):
        print("helm seat: WARN — spawned pane session identity is not yet "
              "proven; SessionStart must bind it before autocompact can act",
              file=sys.stderr)
    _ensure_autocompact_timer()
    # "onboarding SENT" was the overclaim: it named the transport, not the
    # turn. Only DELIVERED earns the success sentence.
    if onboard_state == harness.DELIVERED:
        print("helm seat: spawned %s via %s — pane %s; onboarding submitted "
              "(beacon-arm + @%s work); `helm seat where %s` resolves it"
              % (seat_name, ad.name, handle, seat_name, seat_name))
        return 0
    # UNKNOWN: the pane is up AND REGISTERED — say so, so nobody re-spawns a
    # duplicate — but the brief is unproven, and a spawn that cannot prove its
    # seat was briefed has not finished. rc 1, loudly, in the same spirit as
    # the wake-path leg one function up.
    print("helm seat: spawn of %s via %s INCOMPLETE — pane %s is up and "
          "REGISTERED (do not re-spawn), but its onboarding brief is NOT "
          "PROVEN submitted (%s: %s). Read the pane: if the brief is sitting "
          "unsent in the composer, submit it; the seat is otherwise blank."
          % (seat_name, ad.name, handle, onboard_state, onboard_proof),
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


def cmd_seat(args):
    """seat add|up|down|launch|spawn|where|rebind|resume|smoke|list|status|doctor —
    multimodel seats."""
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
        rc = guard_tail("helm seat doctor", rest, flags=("--ensure", "--json"),
                        usage=_USAGE)
        if rc is not None:
            return rc
        if "--json" in rest and "--ensure" not in rest:
            print("helm seat: --json rides doctor --ensure (the machine-read "
                  "surface); plain doctor is the human one", file=sys.stderr)
            return 2
        return _ensure(rest) if "--ensure" in rest else _doctor(rest)
    if verb == "autocompact":
        from . import autocompact
        return autocompact.cmd_autocompact(rest)
    if verb == "resume-turn":
        from . import resumeturn
        return resumeturn.cmd_resume_turn(rest)
    if verb == "silent-drop":
        from . import silent_drop
        return silent_drop.cmd_silent_drop(rest)
    if verb == "idle-dispatch":
        from . import idle_dispatch
        return idle_dispatch.cmd_idle_dispatch(rest)
    if verb == "spawn":
        if not rest:
            print("usage: helm seat spawn <seat> [--room R] [--cwd DIR] "
                  "[--replace] [--print]", file=sys.stderr)
            return 2
        return _spawn(rest[0], rest[1:])
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
    if verb == "composers":
        rc = guard_tail("helm seat composers", rest, flags=("--json",),
                        usage="seat composers [--json]")
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
        if not rest:
            print("usage: helm seat resume <seat> [--cwd DIR] [--session ID] "
                  "[--force]", file=sys.stderr)
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
                        valued=("--cwd", "--session"),
                        usage="seat resume <seat> [--cwd DIR] [--session ID] "
                              "[--force]")
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
            fn = _up if verb == "up" else _down
            return fn(fam_name, seat=seat_name)
        if verb == "smoke":
            return _smoke(family, multi=multi)
        fam = _require_seat(family)
        if fam is None:
            return 1
        model = rest[rest.index("--model") + 1] if "--model" in rest else None
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
        # guards ride stderr (stdout stays the bare pasteable line), warn
        # never refuse: over-capacity burns one pool faster (fall-through
        # masks it) and a live same-named seat is a relaunch-vs-collision the
        # operator calls (a crashed seat must not brick its slot).
        if inst > 1:
            from . import codexhomes, seats as _seats
            cap = codexhomes.capacity()["total"]
            if inst > cap:
                print("helm seat: WARN — instance %d exceeds pooled fleet "
                      "capacity %d (`helm codex capacity`); the pool falls "
                      "through usage caps but %d concurrent seats burn it "
                      "faster" % (inst, cap, inst), file=sys.stderr)
            row = _seats.roster().get(seat)
            ls = _seats.last_seen(seat, row) if row else None
            if row and ls and time.time() - ls < _seats.QUIET_S:
                print("helm seat: WARN — seat %r already live on the roster "
                      "(last seen %.0fs ago) — relaunch or collision is your "
                      "call" % (seat, time.time() - ls), file=sys.stderr)
        # launch REFRESHES the assets first (G-seatlaunch-installs): delivery
        # hooks + beacon permit + a launch.sh carrying the CURRENT identity
        # shape — retrofitting a seat minted before either existed. stdout
        # stays exactly the pasteable line; notes ride stderr.
        if _write_launch_assets(
                family, _instance_dir(family, seat), room, seat,
                room_source=room_source, multi=multi) \
                is _SEAT_SURFACE_REFUSED:
            return 1
        if seat != family:
            # per-instance proxies: this instance gets its OWN port/config/
            # token/log, so one instance's restart/429-stall never takes a
            # sibling down (the shared-8317 blast-radius). OAuth pool stays
            # family-level — no quota multiplication.
            _mint_instance_proxy(family, seat)
            print("helm seat: %s gets its own proxy — `helm seat up %s` "
                  "(127.0.0.1:%d) before launching"
                  % (seat, seat, _instance_port(family, seat)), file=sys.stderr)
        _ensure_autocompact_timer()
        # the pasteable line: export the bearer from its 0600 file (builtin, no
        # argv), then the env/claude command — the token never transits argv.
        print(_token_export(family, seat) + launch_line(
            family, model, room, seat, room_source=room_source, multi=multi))
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
