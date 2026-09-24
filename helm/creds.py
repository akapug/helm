#!/usr/bin/env python3
"""helm creds / swap — the account scorecard and the rollover rescue, in the
terminal. CLI parity for the quota view: same provider, same truth.

creds  — every account with live headroom/state/reset, duplicate flags.
swap   — a seat ran dry mid-work: find the sessions live on a home, pick the
         healthiest OTHER account of the same provider, and print the exact
         resume block per session. helm prints commands; the human runs them
         (a swap is a seat decision, never an automatic mutation).
"""
import os
import time

from . import homes
from . import providers
from .providers import ProviderError, default_provider

# DERIVED FROM THE PRODUCER, AT RENDER TIME RATHER THAN AT IMPORT. The first cut
# bound this as a module constant, computed once when creds.py is first
# imported — so CRED_STATES could change and the width could not follow, and an
# arm that patched the tuple was reduced to recomputing max() itself and
# asserting its own arithmetic. A review caught that such an arm passes
# against a HARDCODED 13, the exact regression it existed to prevent. A
# function re-reads the tuple every call, so patching the producer moves the
# RENDERED column and an arm can exercise production instead of restating it.
def _state_w():
    """The state cell's width: the longest state the producer can mint.

    Reached through the MODULE (`providers.CRED_STATES`), never through a
    from-import. `from .providers import CRED_STATES` binds the tuple to a name
    in THIS module at import time, so rebinding it on providers would never be
    seen here — the width would silently stop tracking the producer, and an arm
    that patched the producer would prove nothing. That is the same import-time
    staleness that made the previous cut's arm vacuous, one level down."""
    return max(len(s) for s in providers.CRED_STATES)


def _state_cell(state):
    """The state, or a VISIBLY truncated state — never a silently cut one.

    `cred_state` is whatever the provider handed back (creds.py reads
    `st.get("cred_state") or st.get("status") or "?"`), so the column is sized
    to the vocabulary helm KNOWS while the field itself stays open-ended. A
    value longer than the cell therefore remains possible no matter how the
    width is derived, and the ORIGINAL defect was not the missing characters —
    it was that "expired-token" cut to "expired-" reads like a broken string
    rather than a truncation. So an over-long value keeps the width and ends in
    a marker, which a reader can see is an elision and can go look up."""
    width = _state_w()
    text = str(state or "?")
    return text if len(text) <= width else text[:width - 1] + "+"


def _rows():
    prov = default_provider()
    accounts = prov.accounts()
    states = {s.get("account"): s for s in prov.cred_state()}
    windows = {w.get("account"): w for w in prov.windows()}
    out = []
    for a in accounts:
        name = a.get("account") or a.get("name") or "?"
        st = states.get(name, {})
        w = windows.get(name, {})
        hp = st.get("headroom_pct")
        out.append({
            "account": name,
            "provider": a.get("provider") or "?",
            "tier": a.get("tier") or "",
            "headroom": (hp / 100.0) if hp is not None else None,
            "state": st.get("cred_state") or st.get("status") or "?",
            "resets_at_ms": st.get("resets_at_ms"),
            # WHY, NOT JUST WHAT. The provider already distinguishes the reasons
            # an account is UNKNOWN — needs_reauth, http_<code>, network-error,
            # no-credentials — and carries a note explaining what the operator
            # should expect. Both were computed and then dropped here, so the
            # table printed a bare "unknown" that is true and unactionable.
            # Measured 2026-08-26 during a live provider wall: `seat doctor`
            # named the wall to the second while `creds` said unknown seven
            # times out of seven, for accounts whose status field said
            # needs_reauth the whole time.
            "status": st.get("status") or "",
            "note": st.get("note") or "",
            # EVERY WINDOW THE PROVIDER MEASURED, for the families that have
            # more than one. codex pools a 5h AND a 7d budget and the weekly
            # is the one that runs out (task/2480); a single headroom number
            # cannot say which of them is nearly spent.
            "windows": st.get("windows") or [],
            "windows_left": w.get("windows_left"),
            "windows_per_week": w.get("windows_per_week"),
            "verdict": w.get("verdict") or "",
        })
    return out


def _reset_in(ms):
    if not ms:
        return "-"
    s = ms / 1000.0 - time.time()
    if s <= 0:
        return "now"
    if s < 3600:
        return "%dm" % (s // 60)
    return "%.1fh" % (s / 3600)


def _pct(h):
    return "-" if h is None else "%d%%" % round(float(h) * 100)


def _windows_text(windows):
    """'5h 52% used resets 1.5h · 7d 100% used resets 134.4h' — every window a
    provider measured, with the SAME reset formatting the table's reset column
    uses (`_reset_in`), so the two never disagree about what '1.5h' means.

    AND THE PERCENTAGE SAYS WHICH DIRECTION IT RUNS, because the row above it
    runs the OTHER WAY. The field is `used_percent` and the `weekly left`
    column beside it is REMAINING over total, so one line of this scorecard
    showed `0.7/22.3` and `7d 51%` about the same window with opposite
    polarities and neither one labelled. Two unlabelled numbers that disagree
    are worse than either alone: a reader who reconciles them has to guess
    which is which, and the guess is the whole decision."""
    now_ms = time.time() * 1000
    parts = []
    for w in windows:
        secs = w.get("reset_after_seconds")
        when = "-" if secs is None else _reset_in(now_ms + secs * 1000)
        parts.append("%s %.0f%% used resets %s"
                     % (w.get("label") or "?",
                        w.get("used_percent") or 0.0, when))
    return " · ".join(parts)


def cmd_creds(args):
    """creds [crosscheck] — live account scorecard (the quota view, in text);
    crosscheck = the local-session-scan second source vs the header truth."""
    if args and args[0] == "crosscheck":
        from . import localscan
        return localscan.cmd_crosscheck(args[1:])
    if args:
        import sys
        from .cli import suggest
        print("helm creds: unknown verb '%s'%s (creds [crosscheck])"
              % (args[0], suggest(args[0], ("crosscheck",))), file=sys.stderr)
        return 2
    try:
        rows = _rows()
    except ProviderError as e:
        print("helm creds: no quota provider on this machine (%s) — sessions/resume "
              "still work" % e)
        return 1
    if not rows:
        print("helm creds: no accounts found.")
        return 0
    rows.sort(key=lambda r: (r["provider"], -(r["headroom"] or 0)))
    print("helm creds (%d accounts):" % len(rows))
    # THE STATE CELL IS SIZED FROM THE PRODUCER'S OWN VOCABULARY, never from a
    # number written here. `_state_w()` returns max(len(s) for s in
    # providers.CRED_STATES) ON EVERY CALL, so adding a longer state at the
    # place states are MINTED widens this column with no edit here.
    # At the previous width of 8, `expired-token` rendered as "expired-" —
    # dropping the one word that says WHAT expired and reading like a broken
    # string rather than a state. Found in review of the why-unknown
    # surfacing (task/1644): naming the REASON in the verdict cell did nothing
    # for a STATE the column was still mangling; different fields.
    #
    # THE FIRST FIX WAS A HARDCODED 13 WITH A COPY OF THE VOCABULARY IN THE
    # TEST, and a review refused it: a copied tuple is not pinned to anything, so
    # a new longer state left the arm GREEN while production truncated. Hence
    # both the width AND the arm now read the producer.
    print("  %-9s %-30s %-9s %-9s %-*s %-9s %-14s %s" % (
        "provider", "account", "tier", "headroom", _state_w(), "state",
        # THE HEADER SAYS WHICH HALF IT IS. The cell is
        # windows_LEFT/windows_per_week, and every other X/Y a reader meets in
        # this domain is USED/total -- so "weekly" alone invites exactly the
        # wrong reading, and got it: an account at 1.0/8.0 was read as barely
        # touched when it had one window left. A cred plan built on that is
        # backwards for every row it names.
        "reset", "weekly left", "verdict"))
    notes = {}
    for r in rows:
        wl = ("%.1f/%.1f" % (r["windows_left"], r["windows_per_week"])
              if r["windows_left"] is not None else "-")
        # THE VERDICT CELL CARRIES THE REASON WHEN THERE IS NO WINDOWS VERDICT.
        # An account with a real verdict keeps it — that is the stronger fact and
        # is never overwritten. Otherwise the provider's status fills a cell that
        # was empty for exactly the rows a reader is squinting at.
        why = r["verdict"] or r["status"]
        if r["note"]:
            notes.setdefault(r["note"], []).append(r["account"])
        print("  %-9s %-30s %-9s %-9s %-*s %-9s %-14s %s" % (
            r["provider"], r["account"][:30], (r["tier"] or "-")[:9],
            _pct(r["headroom"]), _state_w(), _state_cell(r["state"]),
            _reset_in(r["resets_at_ms"]), wl, why))
        # THE PER-WINDOW LINE IS AN EXTRA LINE, NEVER AN EXTRA COLUMN. A ninth
        # column would re-render every row of every family to say nothing about
        # the ones with a single window; a continuation line under the row it
        # belongs to leaves those rows byte-for-byte as they were.
        if r["windows"]:
            print("      windows: %s" % _windows_text(r["windows"]))
    # ONE FOOTNOTE PER DISTINCT NOTE, not one per row. A stale-token wall hits
    # every account of a family with the SAME note, so per-row printing would
    # bury the table in seven identical sentences; the count carries how wide it
    # is. UNKNOWN STAYS UNKNOWN — the note says why the probe could not answer,
    # and must never be read as a state the probe established.
    for note, accts in sorted(notes.items()):
        print("  note (%d account%s): %s"
              % (len(accts), "s"[:len(accts) != 1], note))
    # THE POINTER, and ONLY when there is something to point at. This table is
    # the MEASURED half — headroom, state, reset — and it structurally cannot
    # see an account no provider watches (an X subscription, a HuggingFace
    # plan). That gap is why agents kept asking the owner how many of each
    # account exist and what each is for, so the table that holds the answer
    # gets named exactly here, in one line. An EMPTY declared inventory prints
    # nothing: a pointer to nothing is noise, and noise on every run is how a
    # footer stops being read.
    line = _declared_pointer()
    if line:
        print("  " + line)
    return 0


def _declared_pointer():
    """accounts.agent_line(), or "" — never a raise and never a stack trace on
    a scorecard. The declared inventory is a different module's file and a
    machine without one is normal; this footer may not be able to fail the
    verb that carries it."""
    try:
        from . import accounts
        return accounts.agent_line()
    except Exception:                      # noqa: BLE001 — a footer never raises
        return ""


def cmd_swap(args):
    """swap <home-or-account> — a seat ran dry: print resume-under-a-healthier-
    account blocks for its live sessions. Never mutates; the human runs them."""
    import sys
    if not args:
        print("usage: helm swap <home-name-or-account-email>", file=sys.stderr)
        return 2
    target = args[0]
    listing = [h for h in homes.homes_list() if not h.get("archived")]
    match = next((h for h in listing
                  if target in (h.get("name"), h.get("identity")) or target in (h.get("aliases") or [])), None)
    if match is None:
        print("helm swap: no cred home matches '%s' (see `helm homes`)" % target,
              file=sys.stderr)
        return 1
    provider = match.get("provider")
    try:
        fam = "anthropic" if provider == "claude" else provider
        rows = [r for r in _rows() if r["provider"] == fam
                and r["account"] != match.get("identity")
                and (r["headroom"] is None or r["headroom"] > 0.1)]
    except ProviderError:
        rows = []
    rows.sort(key=lambda r: -(r["headroom"] or 0))
    if not rows:
        print("helm swap: no alternative %s account with known headroom — "
              "add one (`helm homes prepare %s <email>`)" % (provider, provider))
        return 1
    best = rows[0]
    print("helm swap: healthiest %s alternative: %s (headroom %s, state %s)"
          % (provider, best["account"], _pct(best["headroom"]), best["state"]))
    from . import sessions as sess_mod
    target_home = next((h for h in listing if h.get("identity") == best["account"]
                        and h.get("provider") == provider), None)
    env_var = "CLAUDE_CONFIG_DIR" if provider == "claude" else "CODEX_HOME"
    live = match.get("live_pids") or []
    print("  live pids on '%s': %s" % (match.get("name"),
                                       ",".join(map(str, live)) or "none detected"))
    recent = sess_mod.rows_for(limit=5)
    from_home = [r for r in recent if r["h"] == ("claude" if provider == "claude" else "codex")]
    if not from_home:
        print("  no recent %s sessions in the catalog to re-seat" % provider)
        return 0
    print("  resume blocks (newest %d — run in fresh terminals AFTER stopping the dry seat):"
          % len(from_home[:3]))
    for r in from_home[:3]:
        base = sess_mod.resume_command(r)
        if target_home:
            # inject the env prefix onto the HARNESS clause (the last " && "
            # clause), never a raw substring replace — a cwd containing
            # "claude " must not be corrupted (test-pinned). The clause may
            # carry an env-run prefix (child-stamp unsets) before the harness
            # binary, so anchor on the binary token, not position 0.
            head, sep, tail = base.rpartition(" && ")
            harness_at = tail.find(" " + provider + " ")
            if sep and (tail.startswith(provider + " ") or harness_at > 0):
                base = "%s && env %s=%s %s" % (head, env_var,
                                               target_home.get("path", "?"), tail)
        print("    " + base)
    return 0
