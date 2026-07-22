#!/usr/bin/env python3
"""helm sessions — every local session, all harnesses, keyed to your projects.

The catalog is the row source; the registry is the lens:
sessions group under the helm-known project whose tree their cwd lives in, so
"what was I doing on that project?" is one verb. Resume stays one copy-paste away —
the exact command, not a wrapper (it's just the harness's own CLI).
"""
import glob
import os
import shlex
import stat
import sys
import time

from . import registry


def _project_lens():
    """[(name, prefix)] longest-prefix-first, so nested repos match before
    parents. Uses the registry's full cv_scope prefix set — sibling-dir
    worktree cwds don't share the canonical root's prefix."""
    reg = registry.load()
    pairs = []
    for p in reg["projects"].values():
        if not p.get("path") or p.get("external"):
            continue
        prefixes = (p.get("cv_scope") or {}).get("cwd_prefixes") or [p["path"]]
        pairs += [(p["name"], pre) for pre in prefixes]
    return sorted(pairs, key=lambda t: -len(t[1]))


def _project_for(cwd, lens):
    cwd = os.path.expanduser(cwd or "")
    for name, path in lens:
        if cwd == path or cwd.startswith(path + "/"):
            return name
    return None


def rows_for(project=None, include_synthetic=False, limit=None):
    """Catalog rows via transcripts.get_catalog() — the single-flight cached
    path every other consumer (/api/catalog, /api/burn, the CLI) shares, so
    /api/sessions never re-spawns a full catalog build per GET. Same rows,
    plus cwd overrides applied. Rows are COPIED before the project annotation —
    the cache's row objects are shared and must never be mutated here."""
    from . import transcripts
    rows = transcripts.get_catalog()["rows"]
    lens = _project_lens()
    out = []
    for r in rows:
        proj = _project_for(r.get("cwd") or r.get("c"), lens)
        if project and proj != project:
            continue
        if not include_synthetic and r.get("syn"):
            continue
        out.append(dict(r, project=proj))
        if limit and len(out) >= limit:
            break
    return out


BINDINGS = os.path.expanduser("~/.helm/_global/session-creds.tsv")

# The catch-all home, named once. credhome_for has to know which entry in
# cred_homes() is the fallback, and deriving that from ~ while the home LIST
# comes from elsewhere lets the two disagree silently — the fallback stops
# being recognised as one and starts winning lookups it should lose.
DEFAULT_HOME = "~/.claude"


def cred_homes():
    """Every claude credential home on this machine, DEREFERENCED and deduped.

    ~/.claude-homes carries a short alias symlink beside each real home
    (cto-example -> cto-example-invalid, owner -> owner-example-invalid, …),
    so a naive listdir double-counts every account and makes a one-account
    question look like a two-account one. realpath collapses the pair."""
    roots = [os.path.expanduser(DEFAULT_HOME)]
    homes_dir = os.path.expanduser("~/.claude-homes")
    if os.path.isdir(homes_dir):
        roots += [os.path.join(homes_dir, n) for n in sorted(os.listdir(homes_dir))]
    # proxy seats keep genuinely isolated homes (their projects/ is a REAL dir,
    # not a symlink into the shared one) — they belong in the same search
    seats = os.path.expanduser("~/.helm/_global/seats")
    for pat in ("*/claude", "*/instances/*/claude"):
        roots += sorted(glob.glob(os.path.join(seats, pat)))
    seen, out = set(), []
    for r in roots:
        real = os.path.realpath(r)
        if real not in seen and os.path.isdir(real):
            seen.add(real)
            out.append(real)
    return out


# How much a binding can be trusted, highest first. A binding may only ever be
# OVERWRITTEN BY A STRICTLY BETTER SOURCE — the first version of this index had
# no ranking, so the first guess to arrive won permanently and was consulted
# ahead of the live signal that would have corrected it. A cache that can freeze
# a guess forever and then shadow the truth is worse than no cache.
AUTHORITY = {"environ": 3, "pidrecord": 2, "session-env": 1}


def _bindings():
    """{sid: (home, source)}. Plain TSV because this is append-mostly, read-hot,
    and must stay repairable by eye. Later rows win only if better-sourced."""
    out = {}
    try:
        with open(BINDINGS) as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 2 or not parts[0] or not parts[1]:
                    continue
                sid, home = parts[0], parts[1]
                src = parts[2] if len(parts) > 2 else "session-env"
                prev = out.get(sid)
                if prev is None or AUTHORITY.get(src, 0) >= AUTHORITY.get(prev[1], 0):
                    out[sid] = (home, src)
    except OSError:
        pass
    return out


def record_binding(sid, home, source="session-env"):
    """Freeze a sid -> credhome binding. Best-effort by design: a failure here
    must never break the read that triggered it. A weaker source never
    overwrites a stronger one, and an identical row is not rewritten."""
    if not sid or not home:
        return False
    prev = _bindings().get(sid)
    if prev and (prev == (home, source)
                 or AUTHORITY.get(source, 0) < AUTHORITY.get(prev[1], 0)):
        return False
    try:
        os.makedirs(os.path.dirname(BINDINGS), exist_ok=True)
        with open(BINDINGS, "a") as f:
            f.write("%s\t%s\t%s\n" % (sid, home, source))
        return True
    except OSError:
        return False


def proc_home(pid):
    """The home a LIVE pane is ACTUALLY using, read from its own environment.

    This is ground truth and needs no inference: CLAUDE_CONFIG_DIR is fixed at
    exec and /proc/<pid>/environ reports exactly what the process got. Its
    ABSENCE is equally decisive — it means the pane runs on the default home.
    Every other rung here is archaeology by comparison, and skipping this one is
    how a live pane whose account is stated outright got attributed by guesswork
    instead."""
    try:
        with open("/proc/%d/environ" % pid, "rb") as f:
            env = f.read().decode("utf-8", "replace").split("\0")
    except OSError:
        return None
    for kv in env:
        if kv.startswith("CLAUDE_CONFIG_DIR="):
            val = kv.split("=", 1)[1]
            return os.path.realpath(val) if val else None
    return os.path.realpath(os.path.expanduser(DEFAULT_HOME))


def latch_live():
    """Bind every LIVE pane's session to its home from the AUTHORITATIVE record.

    claude itself writes <home>/sessions/<pid>.json holding {"pid","sessionId"},
    and that file sits in the real home (sessions/ is not one of the symlinked
    dirs), so it is ground truth rather than inference. It exists only while the
    pane lives, which is exactly why it must be harvested rather than queried:
    catch a session while it is running and its account is known forever; miss
    it and you are back to the decaying session-env signal. Returns the number
    of NEW bindings frozen."""
    n = 0
    for home in cred_homes():
        for path in glob.glob(os.path.join(home, "sessions", "*.json")):
            try:
                import json
                with open(path) as f:
                    rec = json.load(f)
            except (OSError, ValueError):
                continue
            if record_binding(rec.get("sessionId"), home, "pidrecord"):
                n += 1
    # the strongest rung last so it OVERWRITES anything weaker already recorded
    for sid, pid in live_sids().items():
        h = proc_home(pid)
        if h and record_binding(sid, h, "environ"):
            n += 1
    return n


def live_sids():
    """{sid: pid} for every session a pane is CURRENTLY holding open.

    Resume has to ask this because a session is not a file you open twice: two
    panes on one sessionId interleave their writes into the same transcript and
    each silently loses turns to the other (the double-open certify-fleet's law
    1 exists to catch). Transcript mtime cannot answer it — a pane that has been
    thinking for an hour looks identical to one that exited an hour ago — so the
    answer comes from the pid-keyed record plus a liveness check on the pid it
    names, never from the file's age."""
    import json
    out = {}
    for home in cred_homes():
        for path in glob.glob(os.path.join(home, "sessions", "*.json")):
            try:
                with open(path) as f:
                    rec = json.load(f)
                pid = int(rec.get("pid") or 0)
            except (OSError, ValueError, TypeError):
                continue
            if pid and rec.get("sessionId") and os.path.isdir("/proc/%d" % pid):
                out[rec["sessionId"]] = pid
    # SECOND RUNG, different failure mode. The pid-keyed record is the better
    # signal but it is not universal: panes launched by a claude older than the
    # feature never write one, and those are precisely the longest-running panes
    # — the ones most likely to still be open and most overdue for a relaunch.
    # A guard whose only rung is the record is therefore blindest exactly where
    # a double-open is most likely. argv carries `--resume <sid>` for any pane
    # started that way, so it covers the gap without depending on the same file.
    for entry in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            with open(entry, "rb") as f:
                argv = f.read().decode("utf-8", "replace").split("\0")
        except OSError:
            continue
        if not argv or not argv[0].endswith("claude"):
            continue
        for flag in ("--resume", "-r"):
            if flag in argv:
                i = argv.index(flag)
                if i + 1 < len(argv) and argv[i + 1] and argv[i + 1] not in out:
                    try:
                        out[argv[i + 1]] = int(entry.split("/")[2])
                    except (ValueError, IndexError):
                        pass
                break
    return out


def credhome_for(sid, latch=True):
    """The credential home that OWNS this session, or None.

    WHY THIS IS NOT DERIVABLE FROM THE TRANSCRIPT PATH: every OAuth credhome
    symlinks projects/ -> ~/.claude/projects, so all their transcripts pile
    into ONE shared directory and the path says nothing about the account.
    (Proxy-seat homes do keep a real projects/ dir — for those the path would
    work — but one rule has to cover both.)

    session-env/<sid> is the per-home artifact that does NOT follow that
    symlink, so it carries the mapping. But it DECAYS — claude prunes it, and
    measured on this machine resolution falls from ~91% for sessions touched in
    the last 2 days to ~18% past 30 days. A signal that erodes cannot answer
    "resume ANY session on ANY cred", so every successful lookup is LATCHED
    into a helm-owned index that never prunes. Coverage then freezes at what we
    could see rather than decaying to nothing, and no new hook is needed —
    reads are frequent enough to be the capture path.

    ~/.claude is consulted LAST and only as a fallback: it accumulates entries
    for sessions that also belong to a named home, so preferring it would
    mis-attribute a named-home session to the default account."""
    # RUNG 0 — a live pane STATES its home; never infer what you can read.
    pid = live_sids().get(sid)
    if pid is not None:
        h = proc_home(pid)
        if h and os.path.isdir(h):
            if latch:
                record_binding(sid, h, "environ")
            return h
    latched = _bindings().get(sid)
    if latched and os.path.isdir(latched[0]):
        return latched[0]
    # RUNG 2 — session-env, and it is NOT EXCLUSIVE. Measured live: one sid was
    # claimed by THREE homes (a default-home pane also had entries under two
    # named homes, written a day later). Returning the first non-default hit made
    # the answer depend on listdir ORDER, and it picked a home the pane had never
    # run on. The creating home is the one whose entry is OLDEST — it was written
    # when the session began — so rank by mtime instead of by iteration order.
    default_home = os.path.realpath(os.path.expanduser(DEFAULT_HOME))
    claims = []
    for home in cred_homes():
        entry = os.path.join(home, "session-env", sid)
        try:
            claims.append((os.path.getmtime(entry), home))
        except OSError:
            continue
    if not claims:
        return None
    claims.sort()
    found = claims[0][1]
    fallback = default_home if any(h == default_home for _, h in claims) else None
    found = found or fallback
    if found and latch:
        record_binding(sid, found, "session-env")
    return found


def resume_command(row, home=None):
    """The harness's own resume invocation. claude resume is cwd-scoped, so the
    command carries the cd; codex resume is global-by-UUID (the cd is comfort).
    A paste-for-human command must be safe in a STAMPED shell: an inherited
    CLAUDE_CODE_CHILD_SESSION/SID/bridge id would make the resumed session a
    subprocess child with transcript persistence silently OFF
    (child-stamp-kills-seat-persistence) — so the line unsets the trio first.

    It also PINS CLAUDE_CONFIG_DIR to the owning home. Without the pin this
    command does not fail on the wrong account — it silently SUCCEEDS on it:
    the shared projects/ symlink means the transcript resolves from any home,
    so the session simply continues under whichever cred the calling shell
    carried. A silent re-home is worse than an error, because nothing ever
    reports it."""
    cwd = os.path.expanduser(row.get("cwd") or "") or "."
    return "cd %r && %s" % (cwd, resume_exec(row, home=home))


def trust_blocked(row, home):
    """The home, when it has NOT accepted the trust dialog for this session's
    cwd — else None.

    This is the second way a resume can look like it worked and deliver
    nothing. claude asks "do you trust the files in this folder?" on first use
    of a directory, and renders that prompt into an ALTERNATE SCREEN BUFFER,
    which the metaharness tail does not capture: the pane reads as blank and
    the process reads as alive, so every liveness signal says healthy while the
    session never loads. Checking the recorded flag turns an invisible hang
    into a refusal that names its own remedy."""
    if row.get("h") != "claude" or not home:
        return None
    cwd = os.path.expanduser(row.get("cwd") or "")
    try:
        import json
        with open(os.path.join(home, ".claude.json")) as f:
            proj = (json.load(f).get("projects") or {}).get(cwd)
    except (OSError, ValueError):
        return None
    if proj is not None and not proj.get("hasTrustDialogAccepted"):
        return home
    return None


def resume_exec(row, home=None, skip_permissions=False):
    """The resume invocation WITHOUT the cd prefix — the part that is safe to
    hand to `exec`.

    Kept separate because gluing `exec` onto the front of the combined
    "cd X && claude …" line silently breaks it: exec binds to `cd`, a shell
    builtin, so the exec fails and the && chain never runs. The pane opens,
    dies immediately, and the spawn still returns a handle — a resume that
    reports success and delivers nothing."""
    from . import seat
    unset = "env -u " + " -u ".join(seat.CHILD_STAMP_VARS) + " "
    if row["h"] != "claude":
        return "%scodex resume %s" % (unset, row["i"])
    if home is None:
        home = credhome_for(row["i"])
    pin = ("CLAUDE_CONFIG_DIR=%s " % home) if is_pinnable(home) else ""
    skip = " --dangerously-skip-permissions" if skip_permissions else ""
    return "%s%sclaude --resume %s%s" % (unset, pin, row["i"], skip)


def is_pinnable(home):
    """Whether CLAUDE_CONFIG_DIR may be pinned to this home.

    The DEFAULT home is the one place where pinning is wrong. ~/.claude is not
    a credential home — it is where claude keeps state when CLAUDE_CONFIG_DIR is
    UNSET, and in that mode the onboarding marker lives one level up in
    ~/.claude.json. Pin CLAUDE_CONFIG_DIR=~/.claude and claude looks for that
    marker at ~/.claude/.claude.json instead, finds a 1.2KB stub with no
    hasCompletedOnboarding, and opens the FIRST-RUN WIZARD rather than the
    session. Measured live: the pane sat on the theme picker forever, alive and
    doing nothing, having reported a successful resume.

    So for the default account, faithfully reproducing the original environment
    means pinning NOTHING. Named homes and proxy-seat homes carry their own
    onboarded .claude.json and pin correctly."""
    if not home:
        return False
    return os.path.realpath(home) != os.path.realpath(os.path.expanduser(DEFAULT_HOME))


RESUME_DIR = os.path.expanduser("~/.helm/_global/resumes")


def mint_resume_script(row, home=None, skip_permissions=False):
    """Write the resume as an executable SCRIPT and return its path.

    TOKEN LAW (borrowed intact from seat.py's resume): what crosses the
    metaharness seam is a PATH, never an expanded command line. A seat's launch
    line carries its proxy token, and a pane command is visible in adapter
    listings, window titles and logs — so the secret must stay in a file the
    adapter only ever names. Native OAuth resumes carry no token, but they go
    through the same door: one rule, no per-caller judgement about whether
    today's line happens to be safe."""
    os.makedirs(RESUME_DIR, exist_ok=True)
    path = os.path.join(RESUME_DIR, "%s.sh" % row["i"])
    cwd = os.path.expanduser(row.get("cwd") or "") or "."
    # cd on its own line, and FAIL LOUD if it cannot: resuming claude from the
    # wrong directory does not error, it forks a fresh session — so a silent
    # fallback to $PWD would look like a resume and lose the history.
    with open(path, "w") as f:
        f.write("#!/bin/sh\n"
                "# helm sessions resume — regenerated on every run;\n"
                "# edit nothing here, it is derived state.\n"
                "cd %s || { echo \"helm resume: cwd is gone: %s\" >&2; exit 1; }\n"
                "exec %s\n"
                % (shlex.quote(cwd), cwd,
                   resume_exec(row, home=home, skip_permissions=skip_permissions)))
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return path


def spawn_resume(row, title=None, home=None, skip_permissions=False):
    """Actually resume the session in a pane. (path, handle, adapter) on
    success; raises harness.HarnessError when no metaharness is reachable.

    This is the hop `sessions` deliberately left out — it computed the exact
    command and handed it over to be pasted. That is a fine contract between
    two programs and a broken one for a human: the owner is GUI-first and does
    not run CLI commands, so a capability that terminates in a string is a
    capability he does not have (human-surface parity). The seat lane already
    owned the real spawn; this routes the universal catalog through it instead
    of reinventing a second launcher."""
    from . import harness
    ad = harness.detect()
    if ad is None:
        raise harness.HarnessError(harness.RECOMMENDATION)
    path = mint_resume_script(row, home=home, skip_permissions=skip_permissions)
    cwd = os.path.expanduser(row.get("cwd") or "") or os.path.expanduser("~")
    if not os.path.isdir(cwd):
        cwd = os.path.expanduser("~")
    title = title or ("resume-" + row["i"][:8])
    try:
        return path, ad.spawn(path, title=title, cwd=cwd), ad.name
    except harness.HarnessError as e:
        # orca resolves its cwd argument as `--worktree path:<cwd>` against its
        # OWN worktree registry, which is commonly empty — so any directory it
        # has not been told about is selector_not_found, and "resume in any cwd"
        # would mean "resume in the handful of cwds orca happens to know".
        # Dropping the selector is safe because the MINTED SCRIPT already cds
        # (and exits non-zero if it cannot): the pane's start directory is
        # cosmetic, the script owns where the session actually resumes.
        if "selector_not_found" not in str(e):
            raise
        return path, ad.spawn(path, title=title), ad.name


def resume_warnings(row):
    """Why THIS resume might not do what you expect — the catalog-level signals
    (make_cmd's provider-coupled preflight is the richer surface; this is the
    same truth the one-paste `sessions resume` path can carry for free). Order:
    a session that cannot resume at all first, then cwd caveats."""
    warn = []
    if row.get("xl"):
        mb = (row.get("z") or 0) / 1e6
        warn.append("OVERSIZED (~%.0fMB in ~%d lines): a single/few-message "
                    "session whose content likely exceeds the 200k window and "
                    "cannot compact — plain resume will fail (common for "
                    "daily-memory/summarizer sessions)." % (mb, row.get("m") or 0))
    elif row.get("syn"):
        warn.append("REFERENCE session (daily-memory summarizer) — a "
                    "read/training artifact, not a resumable work session.")
    if row["h"] == "claude":
        cwd = os.path.expanduser(row.get("cwd") or "")
        if not cwd:
            warn.append("no recorded cwd; claude resume is cwd-scoped — the "
                        "command may not resolve.")
        elif not os.path.isdir(cwd):
            warn.append("recorded cwd no longer exists: %s (resume from another "
                        "dir may fork a fresh session)." % cwd)
    return warn


def _age(mt):
    if not mt:
        return "?"
    d = (time.time() - mt) / 86400.0
    if d < 1:
        return "today"
    return "%dd" % int(d)


def cmd_sessions(args):
    """sessions [<project>] [--limit N] [--all] | sessions resume <id-prefix>"""
    if args and args[0] == "resume":
        if len(args) < 2:
            print("usage: helm sessions resume <session-id-prefix> [--go] [--title T]")
            return 2
        pref = args[1]
        rest = args[2:]
        go = "--go" in rest
        title = rest[rest.index("--title") + 1] if "--title" in rest else None
        hits = [r for r in rows_for(include_synthetic=True)
                if r["i"].startswith(pref)]
        if not hits:
            print("helm sessions: no session id starts with '%s'" % pref)
            return 1
        if len(hits) > 1:
            print("helm sessions: %d sessions match '%s' — disambiguate:" % (len(hits), pref))
            for r in hits[:8]:
                print("  %s  (%s, %s)" % (r["i"], r["h"], r.get("u", "?")))
            return 1
        row = hits[0]
        home = credhome_for(row["i"]) if row["h"] == "claude" else None
        if not go:
            print(resume_command(row, home=home))
            # warnings ride stderr so `$(helm sessions resume <id>)` stays the pure
            # command, while an interactive caller still sees why it might not resume
            for w in resume_warnings(row):
                print("  # " + w, file=sys.stderr)
            if row["h"] == "claude" and not home:
                print("  # no owning credhome found (no session-env/%s entry) — this "
                      "resumes on the CALLING shell's account, which may not be the "
                      "one that created it" % row["i"][:8], file=sys.stderr)
            print("  # add --go to actually open it in a pane", file=sys.stderr)
            return 0
        # --go is REFUSED, not best-effort, when the session cannot resume:
        # spawning a pane that dies on arrival looks like a working resume in
        # every listing while delivering nothing.
        blocking = [w for w in resume_warnings(row) if w.startswith(("OVERSIZED", "REFERENCE"))]
        if blocking:
            for w in blocking:
                print("helm sessions: refusing --go — " + w, file=sys.stderr)
            return 1
        blocked = trust_blocked(row, home)
        if blocked and "--skip-permissions" not in rest:
            print("helm sessions: refusing --go — %s has NOT accepted the trust "
                  "dialog for %s." % (os.path.basename(blocked), row.get("cwd")),
                  file=sys.stderr)
            print("  The pane would open, render the dialog into an alternate screen "
                  "buffer the adapter cannot read, and sit there looking like a "
                  "healthy blank pane forever.", file=sys.stderr)
            print("  Re-run with --skip-permissions to pass "
                  "--dangerously-skip-permissions, or accept the dialog once by hand.",
                  file=sys.stderr)
            return 1
        held = live_sids().get(row["i"])
        if held and "--force" not in rest:
            print("helm sessions: refusing --go — session %s is ALREADY OPEN in pid %d."
                  % (row["i"][:8], held), file=sys.stderr)
            print("  A second pane on one sessionId interleaves both panes' writes into "
                  "the same transcript and each loses turns to the other.", file=sys.stderr)
            print("  Switch to that pane, or pass --force if you know it is a zombie.",
                  file=sys.stderr)
            return 1
        from . import harness
        try:
            path, handle, adapter = spawn_resume(
                row, title=title, home=home,
                skip_permissions="--skip-permissions" in rest)
        except harness.HarnessError as e:
            print("helm sessions: cannot spawn a pane: %s" % e, file=sys.stderr)
            print("  paste instead: " + resume_command(row, home=home), file=sys.stderr)
            return 1
        print("helm sessions: resumed %s via %s — pane %s" % (row["i"][:8], adapter, handle))
        print("  cred: %s" % (home if is_pinnable(home)
                                else "%s (default account — deliberately UNPINNED; "
                                     "pinning it opens the first-run wizard)" % (home or "unknown")))
        print("  cwd : %s" % (row.get("cwd") or "?"))
        print("  script: %s" % path)
        for w in resume_warnings(row):
            print("  warn: " + w, file=sys.stderr)
        return 0

    project = None
    limit = 25
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])
    for a in args:
        if not a.startswith("--") and (not args.index(a) or args[args.index(a) - 1] != "--limit"):
            project = a
    rows = rows_for(project=project, include_synthetic="--all" in args, limit=limit)
    if not rows:
        print("helm sessions: none%s." % (" for project '%s'" % project if project else ""))
        return 0
    scope = " — " + project if project else ""
    print("helm sessions (%d newest%s):" % (len(rows), scope))
    for r in rows:
        proj = r.get("project") or "-"
        print("  %-6s %-7s %-20s %-9s %s" % (
            _age(r.get("mt")), r["h"], proj[:20], r["i"][:8],
            (r.get("t") or "(untitled)")[:70]))
    print("resume any: helm sessions resume <id-prefix>")
    return 0
