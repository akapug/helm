#!/usr/bin/env python3
"""helm doctor — the health check. READ-ONLY always: it reports, it never
repairs (repair verbs live with their legs: sync, drain, interview).

Every check is a small function returning [(level, message), ...]; the report
loop is data-driven over CHECKS (names resolved late, so any check is a test
seam), no if-forest. Exit 0 unless a FAIL.
"""
import os
import re
import sys

from . import home, pk, registry, whoami

OK, WARN, FAIL = "OK", "WARN", "FAIL"

# Adopted-store filename prefixes (the live pkstore classes).
PREFIXES = ("prior-", "prem-", "lex-", "heuristic-")


def check_home():
    """helm home exists, registry.json parses, project count."""
    root = home.helm_home()
    if not os.path.isdir(root):
        return [(WARN, "helm home missing (%s) — run `helm sync` to scaffold it" % root)]
    out = [(OK, "helm home: %s" % root)]
    path = home.registry_path()
    if not os.path.exists(path):
        out.append((WARN, "registry.json missing — run `helm sync`"))
        return out
    reg = pk.read_json(path)
    if not isinstance(reg, dict):
        out.append((FAIL, "registry.json does not parse (%s)" % path))
        return out
    n = len(reg.get("projects") or {})
    out.append((OK, "registry parses: %d project%s" % (n, "s"[:n != 1])))
    return out


def check_authored():
    """The authored registry layer: file parses, entries merge (path-matched
    projection record, or an external anchor). Raw reads only — never triggers
    the load()-time migration."""
    path = home.authored_path()
    if not os.path.exists(path):
        return []
    auth = pk.read_json(path)
    entries = auth.get("projects") if isinstance(auth, dict) else None
    if not isinstance(entries, dict):
        return [(FAIL, "registry-authored.json does not parse (%s)" % path)]
    bad = sorted(n for n, e in entries.items() if not isinstance(e, dict))
    if bad:
        # a null/garbled ENTRY inside a parseable file must report, not crash
        return [(FAIL, "registry-authored.json has non-object entr%s: %s"
                 % ("y" if len(bad) == 1 else "ies", ", ".join(bad)))]
    reg = pk.read_json(home.registry_path())
    projects = (reg.get("projects") if isinstance(reg, dict) else None) or {}
    live = sum(1 for n, e in entries.items()
               if e.get("external") or (n in projects and
                  e.get("path", projects[n].get("path")) == projects[n].get("path")))
    n = len(entries)
    return [(OK, "authored layer: %d entr%s, %d live in merge"
             % (n, "y" if n == 1 else "ies", live))]


_FOLD_SHOW = 6          # names printed inline before "+N more"


def _folded(level, rows, summary):
    """One line for a whole CLASS of finding, or nothing when the class is empty.

    `rows` is [(name, detail)]. The names are what an operator acts on; the
    per-project detail is a path they can reconstruct from the name, so it is
    dropped past the first few rather than printed 193 times."""
    if not rows:
        return []
    names = [n for n, _d in rows]
    shown = ", ".join(names[:_FOLD_SHOW])
    if len(names) > _FOLD_SHOW:
        shown += ", +%d more" % (len(names) - _FOLD_SHOW)
    return [(level, "%d project%s %s: %s"
             % (len(names), ("s" if len(names) != 1 else ""), summary, shown))]


def check_projects():
    """Per registry project: home dir sane (broken symlink = FAIL), repo path
    still on disk (gone = WARN), memory_dir pointer still valid (gone = WARN).

    REPEATED CLASSES ARE FOLDED TO ONE LINE EACH, and that is a correctness
    property of the report, not cosmetics. MEASURED live: `helm doctor`
    printed 208 warnings, of which ~193 were the SAME finding — "home dir
    missing — run `helm sync`" — once per registry project. Buried at line 209
    of 229 was the one warning that mattered: the pre-push leak guard had been
    built, documented, given its own installer, and never installed, leaving 131
    pushes unscanned. A correct detector fired correctly on every run for two
    days and nobody could see it.
    #
    A report nobody can read is a report that does not exist, so a check that
    can emit one line per project is a check that can hide the other findings.
    Folding is what keeps the signal reachable: the operator's ACTION for all
    193 is identical (`helm sync`), so 193 lines carry exactly the information
    of one line plus a count.
    #
    FAIL IS NEVER FOLDED. A broken symlink is individually actionable, rare, and
    the thing you most need named — folding it would trade this bug for a worse
    one. Fold only where the remedy is shared."""
    projects = registry.load().get("projects") or {}
    out = []
    missing_home, path_gone, mem_stale = [], [], []
    healthy = 0
    checked = 0
    for name in sorted(projects):
        rec = projects[name]
        if rec.get("retired"):
            continue
        checked += 1
        issues = len(out) + len(missing_home) + len(path_gone) + len(mem_stale)
        p = home.project_dir(name)
        if not rec.get("external"):
            if os.path.islink(p) and not os.path.exists(p):
                out.append((FAIL, "%s: home is a broken symlink (%s -> %s)"
                            % (name, p, os.readlink(p))))
            elif not os.path.isdir(p):
                missing_home.append((name, p))
        path = rec.get("path") or ""
        if path and not os.path.exists(path):
            path_gone.append((name, path))
        mem = rec.get("memory_dir")
        if mem and not os.path.isdir(mem):
            mem_stale.append((name, mem))
        healthy += issues == (len(out) + len(missing_home) + len(path_gone)
                              + len(mem_stale))
    out.extend(_folded(WARN, missing_home, "with no home dir — run `helm sync`"))
    # The phrase "repo moved or deleted" is load-bearing and stays intact:
    # tests/test_doctor.py asserts on it in two places, and folding a class must
    # not silently change what a reader (or a test) greps for.
    out.extend(_folded(WARN, path_gone,
                       "whose path is gone — repo moved or deleted; "
                       "lineage/archive candidates"))
    out.extend(_folded(WARN, mem_stale, "with a stale memory_dir pointer"))
    if checked:
        out.append((OK, "projects: %d of %d healthy" % (healthy, checked)))
    return out


def check_adoption():
    """A registry project in the adoption map whose helm dir is a REAL dir
    (not the adoption symlink) = WARN — sync kept user data, doctor surfaces it."""
    projects = registry.load().get("projects") or {}
    homes = registry.adopted_homes()
    out = []
    for name in sorted(set(projects) & set(homes)):
        p = home.project_dir(name)
        if os.path.isdir(p) and not os.path.islink(p):
            out.append((WARN, "%s: adoption conflict — helm dir is a real dir, expected "
                        "symlink -> %s" % (name, homes[name])))
    return out


def check_projection_registry():
    """Constitution laws 2+3 as a standing guard, over registry.projections():
    a projection with no declared rebuild/source FAILs (it cannot be safely
    wiped or gitignored), a projection ON DISK whose every declared source is
    gone FAILs unless BOTH the estate has no registered seat and the file
    matches its manifest-declared exact empty genesis. Staleness beyond a row's
    declared freshness horizon WARNs, and any file under the helm home or cache
    root named by NO manifest row is an unclassified SQUATTER (the
    ~/.remember rot class) — the ancestor's one-time squatter eviction, made
    permanent. Read-only: rebuilds stay with their legs (sync / sessions /
    inject / drift); the test suite pins rebuild-and-converge."""
    import time
    try:
        rows, squat = registry.projection_survey()
    except Exception as e:
        return [(WARN, "projection registry unreadable (%s: %s)"
                 % (e.__class__.__name__, e))]
    out = []
    roots = {"home": home.helm_home(), "cache": registry.cache_root()}
    for r in rows:
        if r.get("mutable") is not False:
            out.append((FAIL, "%s: manifest row not mutable:false — every "
                              "projection is read-only-as-truth" % r["name"]))
        if r["kind"] != "projection":
            continue
        missing = [w for w in ("rebuild", "sources") if not r.get(w)]
        if missing:
            out.append((FAIL, "%s: projection with undeclared %s — cannot be "
                              "safely wiped or gitignored"
                        % (r["name"], "/".join(missing))))
            continue
        if r["files"] and not any(os.path.exists(s) for s in r["sources"]):
            # Register state answers whether this estate has ever had a chance
            # to create sources; the row contract answers whether the bytes
            # contain any truth to orphan. BOTH are required to silence FAIL.
            genesis = _is_genesis() and registry.projection_is_genesis(r)
            if genesis:
                out.append((OK, "%s: exact clean genesis — projection files "
                                 "present, every source not yet created; a "
                                 "healthy empty estate, not orphaned" % r["name"]))
            else:
                out.append((FAIL, "%s: ORPHANED — projection on disk but every "
                                  "declared source is gone (%s); the copy just "
                                  "became the only truth" % (r["name"], r["source"])))
            continue
        if r["fresh_days"] and r["files"]:
            try:
                newest = max(os.path.getmtime(os.path.join(roots[r["root"]], f))
                             for f in r["files"])
            except OSError:
                continue
            if time.time() - newest > r["fresh_days"] * 86400:
                out.append((WARN, "%s: stale beyond its %dd freshness horizon "
                                  "— rebuild: %s"
                            % (r["name"], r["fresh_days"], r["rebuild"])))
    n_sq = sum(len(v) for v in squat.values())
    if n_sq:
        first = [os.path.join(roots[k], f) for k in ("home", "cache")
                 for f in squat[k]][:3]
        out.append((WARN, "%d unclassified SQUATTER file%s under the helm "
                          "home/cache (e.g. %s) — `helm projections` lists "
                          "them; classify (manifest row) or evict"
                    % (n_sq, "s"[:n_sq != 1], ", ".join(first))))
    n_proj = sum(1 for r in rows if r["kind"] == "projection")
    out.append((OK, "projection registry: %d rows, %d projection%s, %d squatter%s"
                % (len(rows), n_proj, "s"[:n_proj != 1], n_sq, "s"[:n_sq != 1])))
    return out


def _doctor_ok_path():
    return os.path.join(home.helm_home(), "_global", ".doctor-ok")


def _is_genesis():
    """True while the estate has never had the chance to create the sources
    its projections reference — i.e., NO seat has ever been spawned. Once a
    live seat has existed, sources missing after that point are genuine orphans.

    The stamp-based approach deferred the false-FAIL by exactly one run (a
    cross-family e2e repro: run1 rc=0 writes stamp, run2 rc=1 ORPHANED
    forever). A brand-
    new machine legitimately has no harness stores until seats run — the
    projection IS the promise, not the corruption."""
    try:
        from . import seat as smod
        names, blind = smod.registered_seats()
        if blind:
            # A PARTLY-READABLE TREE IS NOT A NEW MACHINE. Genesis means "no
            # seat has ever existed here"; a walk that could not see the whole
            # tree cannot claim that, and claiming it would hide exactly the
            # orphan this check exists to surface.
            return False
        return not names
    except Exception:
        return False  # unreadable -> genesis FALSE (never hide a real orphan)


def _record_genesis():
    """Called after the first FAIL-free doctor pass — a no-op in the
    seat-counting design. Kept as a trivial wrapper for legacy callers
    and for the control assertion in tests that verifies the stamp path
    still exists for cold-start detection."""
    # pass — genesis is determined by zero registered seats, not a stamp




def check_adopted_store(adopted_dir=None):
    """Adopted-store shape: entry counts by prefix + the prem-/prior- same-slug
    duplicate count (the incomplete-migration leftovers)."""
    d = adopted_dir or home.adopted_memory_dir()
    if not os.path.isdir(d):
        return [(WARN, "adopted store missing (%s) — no live memory dir to adopt" % d)]
    files = [f for f in os.listdir(d)
             if f.endswith(".md") and os.path.isfile(os.path.join(d, f))]
    counts = {p: sum(1 for f in files if f.startswith(p)) for p in PREFIXES}
    other = len(files) - sum(counts.values())
    dups = {f[5:] for f in files if f.startswith("prem-")} \
        & {f[6:] for f in files if f.startswith("prior-")}
    out = [(OK, "adopted store: %s (%s, other=%d)" % (
        d, ", ".join("%s=%d" % (p.rstrip("-"), counts[p]) for p in PREFIXES), other))]
    if dups:
        n = len(dups)
        out.append((WARN, "%d prem-/prior- same-slug duplicate%s (incomplete-migration "
                    "leftovers) — `helm drain --sweep-dups` will archive these, owner-gated"
                    % (n, "s"[:n != 1])))
    return out


def check_lexicon_dead_vocabulary():
    """A multi-word kind: WITHOUT a comma and with no keywords: is a legacy
    mis-filed keywords list the comma-gated fallback cannot rescue (it cannot
    be told from a taxonomy slug) — that entry's symptom vocabulary is dead
    until redefined with the 4-field form."""
    from . import store
    try:
        # include_retired pulls the WHOLE record set (default load_all drops
        # every non-injectable status — candidate/provisional included), so a
        # mis-filed CANDIDATE's dead vocabulary is SEEN; only the tombstoned
        # states (retired/delete_eligible) are genuinely gone and skipped.
        entries = store.load_all(types=("lexicon",), include_retired=True)
    except Exception as e:  # scan trouble is a WARN, never a crash
        return [(WARN, "lexicon scan failed: %s" % e)]
    out = []
    for e in entries:
        if e.get("status") in (store.STATUS_RETIRED, store.STATUS_DELETE_ELIGIBLE):
            continue
        kind = (e.get("kind") or "").strip()
        if len(kind.split()) > 1 and "," not in kind and not e.get("keywords"):
            out.append((WARN, "lexicon '%s': space-separated kind '%s' with no "
                        "keywords — legacy mis-file, its symptom vocabulary is "
                        "DEAD; migrate: helm store add lexicon \"%s | %s | "
                        "phrase | %s\"" % (e["id"], kind, e["id"],
                                           e.get("definition") or "<definition>",
                                           ", ".join(kind.split()))))
    return out


def check_know_your_user():
    """The know-your-user leg: WARN while empty and un-interviewed."""
    p = whoami.load_profile()
    notes = whoami.load_notes()
    if not (p["technical_level"] or p["guidance"] or notes) and p["interview_status"] != "done":
        return [(WARN, "know-your-user: the know-your-user leg is empty — run `helm interview`")]
    return [(OK, "know-your-user: level=%s, %d guidance, %d active note%s (interview %s)" % (
        p["technical_level"] or "-", len(p["guidance"]), len(notes),
        "s"[:len(notes) != 1], p["interview_status"] or "not offered"))]


def check_cv(cv_dir=None):
    """cv presence — the recall plane."""
    d = cv_dir or os.path.join(os.path.expanduser("~"), ".clustervision")
    if os.path.exists(d):
        return [(OK, "cv recall plane live (%s)" % d)]
    return [(WARN, "cv missing (%s) — recall plane offline" % d)]


def check_inject_coverage():
    """The crown-jewel wiring: how many claude homes carry the per-turn inject
    hook (present + resolvable helm + fail-open contract)."""
    from . import hooks
    try:
        rows = hooks.status_rows()
    except Exception as e:
        return [(WARN, "inject coverage unknown (%s: %s)" % (e.__class__.__name__, e))]
    if not rows:
        return []
    n = sum(1 for r in rows if r["hook"] and r["resolvable"] and r["fail_open"])
    msg = "inject coverage: %d of %d claude homes" % (n, len(rows))
    if n < len(rows):
        return [(WARN, msg + " — `helm hooks install` closes the gap")]
    return [(OK, msg)]


def check_hook_scopes():
    """Owned hooks loaded at both home and project scope fire twice.

    Project-only wiring is supported and therefore quiet; an unreadable scan is
    UNKNOWN, never a clean bill. This is read-only — doctor names the defect but
    never guesses which deliberately-authored scope to delete."""
    from . import hooks
    try:
        rows = hooks.project_scope_rows()
    except Exception as e:
        return [(WARN, "project hook scan UNKNOWN (%s: %s)"
                       % (e.__class__.__name__, e))]
    return [(WARN, hooks.project_scope_message(r)) for r in rows
            if r["status"] in ("duplicate", "unknown")]


def check_filesystems():
    """The mount plane (scratch.py): every filesystem helm writes measured on
    BOTH axes — bytes AND INODES. An explicit tmpfs `nr_inodes=` cap is
    invisible to every bytes-based check, which is exactly how the fleet hit
    `No space left on device` on a /tmp reading 36% used. Fail-open."""
    try:
        from . import scratch
        return scratch.doctor_rows()
    except Exception as e:
        return [(WARN, "filesystem check unavailable (%s: %s) — bytes AND "
                       "inode pressure UNKNOWN" % (e.__class__.__name__, e))]


def check_env():
    """HELM_*/MELD_* overrides in effect."""
    out = [(OK, "env override: %s=%s" % (var, os.environ[var]))
           for var in ("HELM_HOME", "MELD_HOME") if os.environ.get(var)]
    return out or [(OK, "no env overrides — home resolves to ~/.helm")]


# The harness versions the physics facts were probed against (physics.py
# header). A harness upgrade makes those facts quietly wrong, never loudly
# broken — this sentinel is the loud part.
PHYSICS_PROBED = {"claude": "2.1.207", "codex": "0.144.1"}


def check_physics_currency():
    """Installed harness version vs the version physics.py was probed against."""
    import shutil as _sh
    import subprocess as _sp
    out = []
    for tool, probed in sorted(PHYSICS_PROBED.items()):
        if not _sh.which(tool):
            continue
        try:
            v = _sp.run([tool, "--version"], capture_output=True, text=True,
                        timeout=15).stdout.strip()
        except (_sp.TimeoutExpired, OSError) as e:
            # the sentinel must not fail QUIET exactly when currency is unknowable
            out.append((WARN, "%s --version failed (%s) — physics facts probed "
                              "at %s, currency UNKNOWN" % (tool, type(e).__name__, probed)))
            continue
        m = re.search(r"\d+\.\d+\.\d+", v)
        if not m:
            out.append((WARN, "%s --version unparseable (%r) — physics facts "
                              "probed at %s, currency unknown" % (tool, v[:40], probed)))
        elif m.group(0) != probed:
            out.append((WARN, "%s is %s but physics.py facts were probed at %s — "
                              "re-verify config-resolution behavior (settings "
                              "precedence, MCP sources) and update PHYSICS_PROBED"
                              % (tool, m.group(0), probed)))
        else:
            out.append((OK, "%s %s matches the physics probe baseline" % (tool, probed)))
    return out


def check_chat_node():
    """The chat room node (chat v2's signed transport): liveness, chain head,
    joined-cell balances (the never-die-on-computrons watch). Read-only —
    repairs live with `helm chat node up`."""
    from . import chat, pk as _pk
    from . import cell as _cell
    url = chat.node_url()
    transport = chat.transport_status()
    signer = _cell.bin_status()
    degraded = ([(WARN, "chat signing " +
                  chat.transport_failure_summary(transport))]
                if transport.get("mode") == "degraded" else [])
    if signer["configured"] and not signer["usable"] \
            and transport.get("code") != "signer_unavailable":
        degraded.append((WARN, "chat signer UNAVAILABLE — %s" %
                         chat._safe_reason(signer["reason"])))
    if not url:
        return degraded + [(OK, "chat: signed transport disabled "
                                "(HELM_CHAT_NODE_URL empty) — v1 RAM room only")]
    head = chat.node_head(url)
    if head is None:
        return degraded + [(WARN, "chat room node UNREACHABLE at %s — posts fall "
                                  "back to [unsigned]; `helm chat node up`" % url)]
    out = [(OK, "chat room node LIVE at %s — chain head %s" % (
        url, head.get("chain_index") if head else "(no receipts yet)"))] + degraded
    if transport.get("mode") == "ready":
        out.append((WARN, "chat signing %s — %s" % (
            chat.transport_label(transport), transport.get("detail") or
            "no committed signing receipt observed for this profile")))
    if not signer["configured"]:
        out.append((WARN, "chat signer OFF (HELM_CELL_BIN unset) — the node "
                          "answers but every post rides [unsigned]; set "
                          "HELM_CELL_BIN or accept unsigned-by-default"))
    cells = _pk.read_json(chat.cells_path(), {}) or {}
    for profile in sorted(cells):
        info = _cell.get_json(url + "/api/cell/" + cells[profile], timeout=3) or {}
        bal = info.get("balance")
        level = WARN if isinstance(bal, int) and bal < 200 else OK
        out.append((level, "chat cell '%s': balance %s%s" % (
            profile, bal if bal is not None else "?",
            " — low (the auto-faucet refunds on next post)" if level == WARN else "")))
    return out


def check_record():
    """The tool-outcome recorder: wired per claude home + state fresh
    (record.py owns the logic)."""
    from . import record
    return record.doctor_rows()


def check_cred_families():
    """Shared-family hygiene (the revocation bomb): byte-identical refresh
    tokens across credential homes = copies of ONE token family — reuse
    detection revokes all of them at once, and a fresh login per home is the
    only fix. Content-hash comparison only (homes.py); token bytes never
    surface anywhere."""
    from . import homes
    try:
        rows = [r for r in homes.homes_list()
                if r.get("authed") and not r.get("archived")]
    except Exception as e:
        return [(WARN, "cred-family audit unavailable (%s: %s)"
                 % (e.__class__.__name__, e))]
    if not rows:
        return []
    out, seen = [], set()
    for r in rows:
        fam = r.get("shared_family")
        if not fam:
            continue
        group = (r["provider"], tuple(sorted([r["name"]] + fam)))
        if group in seen:
            continue
        seen.add(group)
        out.append((FAIL, "shared token family: %s homes [%s] hold BYTE-COPIES of "
                          "one refresh token — reuse detection revokes ALL of them; "
                          "fresh login per home (`helm homes verify` has the detail)"
                    % (r["provider"], ", ".join(group[1]))))
    if not out:
        families = {(r["provider"], r["family"]) for r in rows if r.get("family")}
        out.append((OK, "cred token families: %d distinct across %d authed homes — "
                        "no byte-copies" % (len(families), len(rows))))
    return out


def check_cred_drift():
    """The name-vs-account audit (cred.py): a credential home whose dir NAME
    promises one account while its .claude.json holds ANOTHER — what a
    mid-session `/login` leaves behind. Loud, because every name-keyed verb
    (launch, hooks --home, keepalive's log, attribution) silently believes the
    name. Plus the reversibility gate: an account with no credential snapshot
    cannot be put back after the next eviction."""
    from . import cred
    return cred.doctor_rows()


def _git_install_hint(os_release="/etc/os-release", platform=None):
    """The exact git install command for this box — best-effort distro guess
    from /etc/os-release (ID first, ID_LIKE folded in)."""
    if (platform or sys.platform) == "darwin":
        return "xcode-select --install"
    try:
        with open(os_release) as f:
            text = f.read()
    except OSError:
        text = ""
    ids = []
    for line in text.splitlines():
        if line.startswith(("ID=", "ID_LIKE=")):
            ids += line.split("=", 1)[1].strip().strip('"').lower().split()
    for key, cmd in (("debian", "sudo apt install git"),
                     ("ubuntu", "sudo apt install git"),
                     ("fedora", "sudo dnf install git"),
                     ("rhel", "sudo dnf install git"),
                     ("centos", "sudo dnf install git"),
                     ("arch", "sudo pacman -S git"),
                     ("suse", "sudo zypper install git"),
                     ("alpine", "sudo apk add git")):
        if any(key in i for i in ids):
            return cmd
    return "install git via your distro's package manager"


def check_git():
    """git presence — the substrate under sync's repo scan, capsule, and ship.
    Absent = WARN with the exact install command for this distro (helm itself
    still runs; those legs degrade)."""
    import shutil as _sh
    path = _sh.which("git")
    if path:
        return [(OK, "git on PATH (%s)" % path)]
    return [(WARN, "git not found — sync's repo scan, capsule, and ship degrade; "
                   "install: `%s` (jj/jujutsu is a git-compatible alternative "
                   "on the radar — a future helm may accept either)"
             % _git_install_hint())]


def check_actuator_wiring():
    """Declared detectors/actions have an installed scheduled or hooked reader.

    One folded WARN carries the whole class: emitting one row per missing action
    recreates the warning flood that buried the original pre-push gap.
    """
    from . import wiring
    try:
        data = wiring.actuator_census()
    except Exception as e:
        return [(WARN, "actuator wiring UNKNOWN (%s: %s)"
                 % (e.__class__.__name__, e))]
    issue = wiring.actuator_issue_summary(data)
    if issue:
        return [(WARN, "actuator wiring: " + issue)]
    return [(OK, "actuator wiring: every declared action has an installed "
                 "consumer (%d observed)" % data["consumers"])]


def check_work_guard(root=None):
    """The composed git guard rail (`helm work install-guard`): installed AND
    current in the repo doctor runs from? The pre-commit leg carries the
    never-track staged-set scan — a prior leak proved a suite-run
    guard cannot cover the `git add`->`git commit` window, so a rail-managed
    repo with a missing/stale hook is running without its privacy gate and
    NOBODY SEES IT until the next leak. That silent state is this check's
    whole target: a hook shipped in code but absent from .git/hooks is the
    'built but never wired' class. Silent when cwd is not a git repo, or the
    repo shows no sign of the rail (no owned hook, no <root>-wt/ container)
    — not every repo is rail-managed, and nagging foreign repos gets a check
    switched off."""
    from .work import _guard
    from .work._lanes import find_root
    root = root or find_root()
    if not root:
        return []
    try:
        _base, plan = _guard._guard_plan(root)
    except Exception as e:
        return [(WARN, "work-guard rail state unknown (%s: %s)"
                 % (e.__class__.__name__, e))]
    current, stale, owned = [], [], 0
    for p in plan:
        snap = _guard._path_snapshot(p["target"])
        if snap == ("file", p["script"].encode("utf-8"), 0o755):
            current.append(p["name"])
        else:
            stale.append(p["name"])
            if _guard._owned_hook(snap):
                owned += 1
    for installed, source in sorted(_guard._scanner_assets(root).items()):
        try:
            with open(source, "rb") as f:
                want = ("file", f.read(), 0o644)
        except OSError:
            want = None
        if want is None or _guard._path_snapshot(installed) != want:
            stale.append("scanner:" + os.path.basename(installed))
    if not current and not owned and not os.path.isdir(root + "-wt"):
        return []
    if stale:
        return [(WARN, "git guard rail incomplete in %s — missing/stale: %s; "
                       "`helm work install-guard --apply` closes it (pre-commit "
                       "runs the vacuity advisory then never-track enforcement)"
                 % (root, ", ".join(stale)))]
    return [(OK, "git guard rail current in %s (%s)"
             % (root, ", ".join(current)))]


def check_metaharness(detect=None, which=None):
    """The metaharness seam (helm/harness.py): which pane-op companion drives
    `helm seat resume`. helm is metaharness-AGNOSTIC — none installed is a
    WARN carrying the optional-companion recommendation (orca recommended,
    herdr also supported), never a FAIL. detect/which are test seams."""
    from . import harness
    import shutil as _sh
    detect = detect or harness.detect
    which = which or _sh.which
    try:
        ad = detect()
    except Exception as e:
        return [(WARN, "metaharness detection failed (%s: %s)"
                 % (e.__class__.__name__, e))]
    if ad is None:
        return [(WARN, harness.RECOMMENDATION)]
    others = sorted(n for n, cls in harness.ADAPTERS.items()
                    if n != ad.name and which(cls.bin))
    return [(OK, "metaharness: %s (%s) — pane ops (seat resume) live%s"
             % (ad.name, ad.path,
                "; also present: " + ", ".join(others) if others else ""))]


CHECKS = ("check_home", "check_actuator_wiring",
          "check_authored", "check_projects", "check_adoption",
          "check_projection_registry",
          "check_adopted_store", "check_lexicon_dead_vocabulary",
          "check_know_your_user", "check_cv", "check_filesystems",
          "check_inject_coverage", "check_hook_scopes", "check_env",
          "check_physics_currency", "check_record",
          "check_chat_node", "check_cred_families", "check_cred_drift", "check_git",
          "check_work_guard", "check_metaharness")


def cmd_doctor(args):
    """doctor — read-only health report over the whole helm estate."""
    results = [r for name in CHECKS for r in globals()[name]()]
    for level, msg in results:
        print("  %-4s %s" % (level, msg))
    tally = {lvl: sum(1 for l, _ in results if l == lvl) for lvl in (OK, WARN, FAIL)}
    print("helm doctor: %d ok, %d warn, %d fail" % (tally[OK], tally[WARN], tally[FAIL]))
    if not tally[FAIL]:
        _record_genesis()
    return 1 if tally[FAIL] else 0
