#!/usr/bin/env python3
"""helm doctor — the health check. Report-only by default; explicit --ensure
backs up and heals credentials through their owner before reporting health.

Every check is a small function returning [(level, message), ...]; the report
loop is data-driven over CHECKS (names resolved late, so any check is a test
seam), no if-forest. Exit 0 unless a FAIL.
"""
import os
import time
import re
import sys

from . import home, pk, projscope, registry, whoami

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


_CELL_DRILLDOWN = "`helm chat node status` lists every cell and its balance"

_FOLD_SHOW = 6          # names printed inline before "+N more"


_RENDER_UNSAFE = re.compile(r"[\x00-\x1f\x7f\u2066-\u2069\u202a-\u202e]")


def _safe(name, cap=64):
    """One externally-authored name, made safe to print and bounded.

    NAMES IN THIS REPORT ARE NOT HELM'S TEXT. A chat profile comes from
    HELM_CELL_PROFILE and is persisted verbatim; a project key comes from the
    registry. Both reach a terminal through this report, so control characters,
    bidi overrides and unbounded length are an operator-output problem before
    they are a cosmetic one — a name can otherwise move the cursor, reverse the
    line, or push every other finding off the screen by itself. chatnode.py
    states the same rule for the signer's strings: neither is helm's text, so
    neither reaches a terminal unsanitised.
    """
    text = _RENDER_UNSAFE.sub("?", str(name))
    return text if len(text) <= cap else text[:cap - 1] + "…"


def _summary_names(names, drilldown, cap):
    """A bounded name list that ALWAYS carries a way to reach what it omitted.

    A TRUNCATING SUMMARY HAS EXACTLY TWO HONEST FORMS: render every identity,
    or name an EXECUTABLE surface that renders them. Nothing else is honest,
    because a stable sort makes any silent tail permanently invisible — the
    same rows hidden on every run — while an unbounded join lets one population
    bury every other finding in the report. `drilldown` is REQUIRED rather than
    optional so a caller cannot pick the third, dishonest option by omission.
    """
    safe = [_safe(n) for n in names]
    if len(safe) <= cap:
        return ", ".join(safe)
    return "%s, +%d more — %s" % (", ".join(safe[:cap]), len(safe) - cap,
                                  drilldown)




def _folded(level, rows, summary, drilldown):
    """One line for a whole CLASS of finding, or nothing when the class is empty.

    `rows` is [(name, detail)]. The names are what an operator acts on; the
    per-project detail is a path they can reconstruct from the name, so it is
    dropped past the first few rather than printed 193 times."""
    if not rows:
        return []
    names = [n for n, _d in rows]
    shown = _summary_names(names, drilldown, _FOLD_SHOW)
    return [(level, "%d project%s %s: %s"
             % (len(names), ("s" if len(names) != 1 else ""), summary, shown))]


def check_projects():
    """Per registry project: home dir sane (broken symlink = FAIL), repo path
    still on disk (gone = WARN), memory_dir pointer still valid (gone = WARN).

    REPEATED CLASSES ARE FOLDED TO ONE LINE EACH, and that is a correctness
    property of the report, not cosmetics. MEASURED 2026-07-31: `helm doctor`
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
    try:
        projects = registry.load(strict=True).get("projects") or {}
    except (OSError, ValueError):
        projects = {}
    out = []
    missing_home, path_gone, mem_stale = [], [], []
    unguarded, guard_drift, merge_blind = [], [], []
    healthy = 0
    checked = 0
    for name in sorted(projects):
        rec = projects[name]
        if rec.get("retired"):
            continue
        checked += 1
        issues = (len(out) + len(missing_home) + len(path_gone)
                  + len(mem_stale) + len(unguarded) + len(guard_drift)
                  + len(merge_blind))
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
        state = _repo_guard_state(path)
        if state == "unguarded":
            unguarded.append((name, path))
        elif state == "merge-blind":
            merge_blind.append((name, path))
        elif state == "drift":
            guard_drift.append((name, path))
        healthy += issues == (len(out) + len(missing_home) + len(path_gone)
                              + len(mem_stale) + len(unguarded)
                              + len(guard_drift) + len(merge_blind))
    out.extend(_folded(WARN, missing_home, "with no home dir — run `helm sync`",
                        "`helm projects` lists them all"))
    # The phrase "repo moved or deleted" is load-bearing and stays intact:
    # tests/test_doctor.py asserts on it in two places, and folding a class must
    # not silently change what a reader (or a test) greps for.
    out.extend(_folded(WARN, path_gone,
                       "whose path is gone — repo moved or deleted; "
                       "lineage/archive candidates",
                       "`helm projects` lists them all"))
    out.extend(_folded(WARN, mem_stale, "with a stale memory_dir pointer",
                        "`helm projects` lists them all"))
    # THE GUARD CENSUS IS FLEET-WIDE OR IT IS NOTHING. check_work_guard judges
    # the repo doctor runs from, and stays silent for a repo that shows no
    # sign of the rail — which is exactly the repo that never had a guard.
    # A registry project is helm-run by declaration, so for those the silence
    # is the finding: history is forever, and a repo with no pre-commit leg is
    # one `git add` away from carrying a customer export it cannot un-carry.
    out.extend(_folded(WARN, unguarded,
                       "with NO git leak guard installed — exports, dumps and "
                       "address lists can enter history unseen; "
                       "`helm work install-guard --apply --profile leak "
                       "--repo <path>` arms each",
                       "`helm projects` lists them all"))
    # A GUARD WITH NO pre-merge-commit LEG NEVER SEES A MERGE COMMIT: git
    # builds the commit of a merge it completes itself through that hook and
    # never through pre-commit, so every blob a `git merge --no-ff` brings in
    # lands unscanned. Its own class, because "stale" undersells it — the
    # commit door is armed and the merge door beside it is open.
    out.extend(_folded(WARN, merge_blind,
                       "whose git guard NEVER SEES A MERGE COMMIT — no "
                       "executable pre-merge-commit hook, so a "
                       "`git merge --no-ff` lands "
                       "what pre-commit would refuse; "
                       "`helm work install-guard --apply --repo <path>` arms "
                       "each",
                       "`helm projects` lists them all"))
    out.extend(_folded(WARN, guard_drift,
                       "with a stale, partial or unreadable git guard; "
                       "`helm work install-guard --apply --repo <path>` "
                       "refreshes each",
                       "`helm projects` lists them all"))
    if checked:
        out.append((OK, "projects: %d of %d healthy" % (healthy, checked)))
    return out


def _repo_guard_state(path):
    """'guarded' | 'unguarded' | 'merge-blind' | 'drift' | None for a
    registry project path: None when the path is not a git work tree (nothing
    to guard), 'unguarded' when every planned hook is MISSING, 'merge-blind'
    when the repo's hooks dir has no pre-merge-commit hook git will run
    (absent, or present and not executable) while some other planned hook is
    there, 'drift' when any other planned hook or scanner is STALE or
    UNKNOWN. The predicate is stale_guard_hooks — the
    same one check_work_guard reads — under whatever profile the repo
    declared, so a leak-profile repo is judged against its three hooks and a
    rail repo against six."""
    if not path or not os.path.isdir(path):
        return None
    from .work import _guard
    from .work._lanes import find_root
    try:
        root = find_root(path)
    except Exception:
        root = None
    if not root or os.path.realpath(root) != os.path.realpath(path):
        return None
    try:
        findings = _guard.stale_guard_hooks(root)
        plan = _guard._guard_plan(root)[1]
    except Exception:
        return "drift"
    if not findings:
        return "guarded"
    # UNGUARDED means every hook the repo's profile PLANS is absent — the
    # repo has nothing armed. stale_guard_hooks lists only the non-fresh
    # entries, so "all listed are MISSING" is not that: a rail repo with one
    # scanner snapshot gone would read as unguarded and be told to install
    # the leak profile, a DOWNGRADE. Count the planned hooks themselves.
    planned = [p["name"] for p in plan]
    missing = {n for st, n, _w in findings if st == "MISSING"}
    if all(n in missing for n in planned):
        return "unguarded"
    # BLIND IS WHAT GIT WILL NOT RUN: an absent hook, and a present one that
    # is not executable, whatever its bytes — git skips both on a merge.
    merge_hook = [p["target"] for p in plan if p["name"] == "pre-merge-commit"]
    if merge_hook and not any(os.access(t, os.X_OK) for t in merge_hook):
        return "merge-blind"
    return "drift"


def check_adoption():
    """A registry project in the adoption map whose helm dir is a REAL dir
    (not the adoption symlink) = WARN — sync kept user data, doctor surfaces it."""
    try:
        projects = registry.load(strict=True).get("projects") or {}
    except (OSError, ValueError):
        projects = {}
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

    The stamp-based approach deferred the false-FAIL by exactly one run (an
    e2e repro: run1 rc=0 writes stamp, run2 rc=1 ORPHANED forever). A brand-
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


# A FORWARD-LOOKING CONDITION, NEVER A PROVENANCE MENTION. "filed as
# task/2585" records WHERE a decision lives and stays true forever; "until
# task/2585 lands, do X by hand" is a PROMISE ABOUT THE FUTURE that expires
# without anyone editing it. Only the second shape is scanned, which is why
# this is a phrase list rather than a bare citation count.
#: THE PROMISE VOCABULARY, and it is a SET rather than a phrase because the
#: same promise is spelled a dozen ways. A pattern built from whichever
#: instance is in front of you reads CLEAN over every other spelling, silently
#: — these three canonical forms are the ones it proves nothing about: "until task/N
#: lands", "while task/N is pending", "task/N is not yet available". The
#: clauses fall into two families — a DEADLINE that names the row's landing
#: ("until/once/when ... lands|ships|is done"), and a STANDING STATE that
#: names its absence ("while ... is pending|open", "not yet ..."). Both are
#: promises about work still ahead, which is the property this rung measures.
#: THE GRAMMAR IS BUILT FROM FRAGMENTS, AND A SPACE IN A FRAGMENT IS ANY RUN
#: OF WHITESPACE. Docs wrap: "Until\ntask/2573 lands" is the clause "Until
#: task/2573 lands" with a line break in it, and a pattern written with literal
#: spaces reads the wrapped spelling as no clause at all. `_ws` rewrites every
#: space so the two spellings are one. The alternation is bounded by `\b` on
#: both sides so "for now" is not found inside "for nowhere".
def _ws(fragment):
    return fragment.replace(" ", r"\s+")


_ROW = r"`?task/[0-9]+`?"
_PRONOUN = r"(?:it|that|this|they)"
_LANDS = r"(?:lands|ships|arrives|is (?:done|available|landed|shipped))"
_PENDING = r"(?:is |remains )?(?:pending|open|unlanded|outstanding)"
_FUTURE_CONDITION = re.compile(r"\b(?:" + _ws(
    r"until then|"
    r"(?:until|once|when) (?:" + _ROW + "|" + _PRONOUN + ") " + _LANDS + "|"
    r"while (?:" + _ROW + "|" + _PRONOUN + ") " + _PENDING + "|"
    r"in the meantime|meanwhile|for now|for the moment|"
    r"not yet landed|not yet available|not yet shipped|"
    r"is not yet (?:landed|available|shipped|done)") + r")\b", re.I)
#: THE ANAPHORIC HALF OF THAT SET — clauses whose referent is NOT in their own
#: clause. "Until then" names no row; the "then" points BACK at a landing
#: someone just described, so the real construction routinely puts the citation
#: in one sentence and this in the next. Measured on the live tree: the only
#: true instance in the corpus is exactly that shape, so a rule requiring both
#: halves in one sentence sees ZERO of it.
#: THIS SET IS OPEN AND THE BLOCK LIST ABOVE IS CLOSED, which is the one
#: asymmetry worth stating plainly. A missed BLOCK TYPE is a defect: the list
#: is finite and enumerable. A missed PHRASING is a disclosed limit: English
#: has no closed list of ways to point back at a landing, so this rung buys
#: precision with recall on purpose. The failure direction is what makes that
#: the right trade -- a false warn sends a reader to edit a sentence that is
#: correct, while a miss leaves rot that the next reader of that file may
#: still catch. Add a phrasing when it is MEASURED in this tree, never because
#: it is imaginable.
_ANAPHORIC_CONDITION = re.compile(r"\b(?:" + _ws(
    r"until then|in the meantime|meanwhile|for now|for the moment|"
    r"(?:until|once|when) " + _PRONOUN + " " + _LANDS + "|"
    r"while " + _PRONOUN + " " + _PENDING) + r")\b", re.I)
#: A CITATION THAT POINTS AT A RECORD IS NOT A PROMISE ABOUT ONE, and this is
#: what keeps sentence-ADJACENCY from marrying a provenance note to whatever
#: caveat happens to follow it. "filed against task/N" stays true forever, so
#: the sentence after it is somebody else's business.
#: AND THE VERB IS A WHOLE TOKEN. "unfiled against task/N" is the opposite
#: claim and "oversee task/N" is a different word; matching inside them read
#: a promise as a pointer and silenced it. A `re-` prefix is the same act
#: repeated and stays provenance.
#: THE SEPARATOR IS NOT ALWAYS A SPACE. Docs in this tree spell citations as
#: `task/N` in backticks, in quotes, and in parentheses, and a provenance test
#: keyed on a bare space fails on every one of them -- so "filed against
#: `task/N`" read as a PROMISE about that row rather than a pointer at it.
#: That is the expensive direction: it warns about a sentence that is correct.
_CITE_GAP = r"[\s`'\"(\[]+"
_PROVENANCE_CITE = re.compile(
    r"\b(?:re)?(?:filed|recorded|tracked|logged|captured|noted)" + _CITE_GAP +
    r"(?:against|as|under|in|at)" + _CITE_GAP +
    r"(?:\S+\s+){0,2}?`?task/([0-9]+)`?|"
    r"\b(?:see|per|cf\.?|ref)" + _CITE_GAP + r"`?task/([0-9]+)`?", re.I)
#: A sentence ends at ., ! or ? followed by whitespace, at a semicolon, or at
#: a blank line. Markdown list items and headings are their own sentences: a
#: bullet is a complete thought and the next bullet is a different one.
#: A SEMICOLON IS A CLAUSE BOUNDARY FOR THE SAME REASON A FULL STOP IS. "The
#: change was filed against task/N; separately, the signer is not yet
#: available" names a row in one clause and makes a promise about something
#: else in the next, and reading it as one sentence marries the two.
#: A COMMA IS A CLAUSE BOUNDARY WHEN WHAT FOLLOWS IT STARTS A CLAUSE: a
#: subordinator ("while the signer is not yet available") or a pointer
#: ("per task/N"). Without that, a provenance citation and a promise about
#: something else sat in one clause and the pointer read as the subject.
_CLAUSE_LEAD = (r"while|until|once|when|and|but|so|then|because|although|"
                r"though|per|see|cf\.?|ref|via")
_SENTENCE_SPLIT = re.compile(
    r"(?<=[.!?])\s+|(?<=;)\s*|,\s*(?=(?:%s)\b)" % _CLAUSE_LEAD
    + r"|\n\s*\n|\n\s*(?=[-*+]\s|#)", re.I)
#: A FULL STOP INSIDE AN ABBREVIATION ENDS NO SENTENCE. "e.g. its automatic
#: exit" is one clause with what follows it, and splitting there separates a
#: citation from the condition written about it; "cf. task/N" split after
#: "cf." leaves a two-letter sentence beside a bare citation, which the
#: anaphoric rule then reads as a promise. The list is the abbreviations
#: MEASURED in this corpus, never a dictionary.
_ABBREVIATION = re.compile(r"\b(?:e\.g|i\.e|cf|vs|viz|approx)\.$", re.I)
#: A CITATION IS A WHOLE TOKEN. `subtask/2573`, `task/25730` and `task/2573a`
#: cite nothing this rung can look up, and matching inside them warned about
#: rows that were never named.
_TASK_CITE = re.compile(r"(?<![A-Za-z0-9_/-])task/([0-9]+)(?![0-9A-Za-z_])")


def _subject_cites(text):
    """The rows a sentence PROMISES something about, in order, once each.

    Per OCCURRENCE, never per id. "The proposal was filed against task/N, and
    task/N will automate the exit" cites N as provenance and then as a
    promise, and excluding the id would silence the promise because the
    pointer came first. An occurrence is provenance when it sits inside a
    provenance match; every other occurrence is a subject."""
    spans = [m.span() for m in _PROVENANCE_CITE.finditer(text)]
    return list(dict.fromkeys(
        m.group(1) for m in _TASK_CITE.finditer(text)
        if not any(a <= m.start() and m.end() <= b for a, b in spans)))
#: STRUCTURE THAT IS NOT PROSE, ENUMERATED ONCE RATHER THAN ONE FINDING AT A
#: TIME. Every round of this rung has been "the segmenter also misses X", which
#: is what hand-rolling a Markdown reader looks like from inside. The block
#: STARTS are a closed list in CommonMark, so the list is written out here in
#: full and each one is cited: what is NOT closed is the natural-language
#: vocabulary below, and that difference is the whole reason this comment
#: exists. A block type this misses is a bug; a promise phrasing it misses is
#: a disclosed limit.
_TABLE_ROW = re.compile(r"^\s*\|")
#: `---|---`, with or without the leading pipe: the delimiter row is what makes
#: the lines around it a TABLE rather than prose containing a pipe.
_TABLE_RULE = re.compile(r"^[ :|-]*\|[ :|-]*$|^[ :-]*\|[ :|-]*$")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|[0-9]+[.)])\s+")
#: A SETEXT UNDERLINE MAKES THE PARAGRAPH ABOVE IT A HEADING, so it ends that
#: block exactly as an ATX `#` line starts a new one. Missing it let an
#: anaphor reach back across a heading into the paragraph before it.
_SETEXT = re.compile(r"^\s{0,3}(=+|-{2,})\s*$")
#: `***`, `---`, `___` -- a thematic break is a block boundary, and `---` is
#: also the front-matter fence every SKILL.md in this tree opens with.
_THEMATIC = re.compile(r"^\s{0,3}((\*\s*){3,}|(-\s*){3,}|(_\s*){3,})$")
_BLOCKQUOTE = re.compile(r"^\s{0,3}>")
_HTML_BLOCK = re.compile(r"^\s{0,3}<[!/a-zA-Z]")
#: CommonMark's raw-text elements: the block runs to the CLOSING TAG, across
#: blank lines, and nothing inside it is prose. The end condition is the
#: LITERAL string `</script>` (or `</pre>`, `</style>`, `</textarea>`) on a
#: line -- `</script >` with an interior space closes nothing, so admitting
#: one ended the block early and scanned the lines after it as prose.
_RAW_TEXT_ELEMENT = re.compile(r"<(script|style|pre|textarea)\b", re.I)
#: AN HTML COMMENT IS NOT PROSE WHEREVER IT SITS, and it may open in the
#: middle of a line of prose that IS. Masking it before the block reader runs
#: answers both shapes with one mechanism: the comment's characters become
#: spaces, so the prose around it keeps its own offsets and line numbers, and
#: a comment nobody closed masks to the end of the body.
_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.S)


def _mask_comments(body):
    return _COMMENT.sub(
        lambda m: "".join("\n" if c == "\n" else " " for c in m.group(0)),
        body)
#: A fence is THREE OR MORE of its character and closes on a run at least as
#: long of the SAME character; the info string after the opener is not part of
#: it. Matching only exactly three, or either character, both let real code
#: through as prose.
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")
#: FOUR SPACES OR A TAB IS CODE, but only where a paragraph is not already
#: open -- indented code cannot interrupt a paragraph, and treating a wrapped
#: continuation line as code would silently cut a sentence in half.
_INDENTED = re.compile(r"^(?: {4}|\t)")


def _prose_blocks(body):
    """[(text, [(offset, lineno)])] — the PROSE blocks of a Markdown file.

    A SENTENCE SPLITTER POINTED AT MARKDOWN READS STRUCTURE AS GRAMMAR, and
    the ways it is wrong do not cancel each other. Fenced and indented code is
    code, so a `task/N` in a sample becomes a promise nobody made. A table row
    is cells, so two unrelated columns join into one sentence. A hard-wrapped
    paragraph is ONE sentence spread over lines. Two list items are two
    thoughts that merely happen to be adjacent — and adjacency is what the
    anaphoric rule trades on, so reading them as neighbours lets "until then"
    reach back into a bullet, a heading or a code sample it was never about.

    SO THE TEXT IS SEGMENTED FIRST AND READ SECOND, against the full list of
    block starts rather than the ones a previous round happened to hit.

    EACH BLOCK CARRIES THE OFFSET MAP OF ITS OWN SOURCE LINES, so a location
    is computed from where the text CAME FROM rather than by searching the
    document for a copy of it. The two differ whenever a document repeats a
    sentence, and the search answer is always the FIRST copy."""
    blocks, cur = [], []

    def flush():
        if cur:
            text, offs, pos = "\n".join(t for _, t in cur), [], 0
            for lineno, t in cur:
                offs.append((pos, lineno))
                pos += len(t) + 1
            blocks.append((text, offs))
        del cur[:]

    fence = None
    in_table = False
    html = None              # a raw-text element (script/style/pre/textarea)
    #                          runs to its closing tag, any other tag block to
    #                          a blank line
    for lineno, raw in enumerate(_mask_comments(body).split("\n"), 1):
        s = raw.strip()
        if fence is not None:
            # A FENCE CLOSES ON ITS OWN CHARACTER, at least as long as the
            # opener. Everything until then is code however much it reads
            # like a sentence.
            m = _FENCE.match(raw)
            # A CLOSING FENCE CARRIES NOTHING AFTER IT. "``` not a closer" is
            # a line of the code block, so the block runs on and the prose
            # after it is still code.
            if m and m.group(1)[0] == fence[0] \
                    and len(m.group(1)) >= len(fence) \
                    and not m.group(2).strip():
                fence = None
            continue
        m = _FENCE.match(raw)
        if m:
            flush()
            fence = m.group(1)
            continue
        if html == "tag":
            if not s:
                html = None     # the blank line ends the block and is consumed
            continue
        if html is not None:                 # a raw-text element
            if re.search(r"</%s>" % html, raw, re.I):
                html = None
            continue
        if not s:
            in_table = False
            flush()
            continue
        if _TABLE_RULE.match(s) and ("|" in s):
            # THE DELIMITER ROW RETROACTIVELY MAKES ITS HEADER A TABLE ROW.
            # The header is already sitting in `cur` and contains no leading
            # pipe in the common spelling, so without this it stays prose.
            if cur and "|" in cur[-1][1]:
                cur.pop()
            in_table = True
            flush()
            continue
        if in_table and "|" in s:
            continue
        in_table = False
        if _SETEXT.match(raw) and cur:
            flush()          # the paragraph above was a HEADING
            continue
        if _HTML_BLOCK.match(raw):
            # AN HTML BLOCK IS EVERY LINE TO ITS END, not the line that opened
            # it. A comment ends on the line carrying `-->`; a tag block ends
            # at the blank line. Flushing only the opening line let the body
            # of a multi-line comment read as prose.
            flush()
            m = _RAW_TEXT_ELEMENT.match(raw.lstrip())
            if m:
                if not re.search(r"</%s>" % m.group(1), raw, re.I):
                    html = m.group(1).lower()
            else:
                html = "tag"
            continue
        if _THEMATIC.match(raw) or _BLOCKQUOTE.match(raw) \
                or _TABLE_ROW.match(s):
            flush()
            continue
        if _INDENTED.match(raw) and not cur:
            continue         # indented code, which cannot interrupt a paragraph
        if s.startswith("#"):
            # AN ATX HEADING IS ITS OWN BLOCK ON BOTH SIDES. Flushing only
            # above it let the heading and the paragraph under it share a
            # block, so a row named in the heading anchored a promise made
            # in the body about something else.
            flush()
            cur.append((lineno, raw))
            flush()
            continue
        if _LIST_ITEM.match(s):
            flush()
        cur.append((lineno, raw))
    flush()
    return blocks


def _sentences(text):
    """[(offset, clause, sentence_no)] — the split, with every piece's ORIGIN
    kept and the SENTENCE it belongs to numbered.

    `re.split` discards positions, and recovering them afterwards with `find`
    is what makes a repeated sentence report the wrong line. Blank pieces are
    dropped so an empty split does not become a phantom neighbour for the
    anaphoric rule.

    A semicolon starts a new CLAUSE, not a new sentence: "once it lands; until
    then do it by hand" is one stale promise written twice, and warning for
    each clause inflates the count a reader judges the estate by. The clause
    is the unit of ANCHORING (which row a condition is about); the sentence is
    the unit of REPORTING (one warning per row per sentence)."""
    out, pos, sentence = [], 0, 0
    for m in _SENTENCE_SPLIT.finditer(text):
        if m.group(0).count("\n") < 2 \
                and _ABBREVIATION.search(text[pos:m.start()]):
            continue                        # "e.g. " ends no sentence
        out.append((pos, text[pos:m.start()], sentence))
        # THE SEPARATOR DECIDES, NOT THE PIECE. A comma separator is consumed
        # by the match, so the clause it ended does not carry it -- reading
        # the piece's last character counted every comma clause as a new
        # SENTENCE, and one row promised in three clauses was warned three
        # times instead of once.
        if not re.match(r"[,;]", m.group(0)) \
                and not text[pos:m.start()].rstrip().endswith(";"):
            sentence += 1
        pos = m.end()
    out.append((pos, text[pos:], sentence))
    return [(o, t, n) for o, t, n in out if t.strip()]


def _lineno(offsets, pos):
    """The source line a block-relative offset came from."""
    found = offsets[0][1]
    for off, lineno in offsets:
        if off > pos:
            break
        found = lineno
    return found


def check_doc_task_conditionals(root=None, status_of=None):
    """A doc that says "until task/N lands, do X by hand" goes WRONG when N
    lands, and nothing in the tree notices.

    THE SENTENCE IS SELF-CONSISTENT BOTH BEFORE AND AFTER, which is why no
    reviewer catches it: the file reads exactly the same the day the cited row
    closes. Only a join against the task store can see the rot, and that join
    is live state, so it belongs here rather than in a hermetic arm.

    THE FAILURE DIRECTION IS WHAT MAKES IT WORTH A RUNG. The stale branch is
    the DANGEROUS one — measured on the seat-a-project skill, where a closed
    row left a sentence routing a fresh agent to hand-exit a LIVE AGENT'S pane
    while the one-verb door it said was unavailable shipped in the same tree.

    IT SCANS A WINDOW, NOT A LINE, because the construction splits across them
    — the citation sat on one line and "once it lands; until then" on the next,
    so a line-anchored pattern found ZERO of the real instance.

    A CLOSED citation and an UNRESOLVABLE one get DIFFERENT sentences: a row
    this reader cannot look up is not a row that is fine.

    WARN rather than FAIL deliberately: this is documentation rot with a named
    hazard, not a fleet outage, and `doctor --quiet` is read by automation that
    should not go red over prose. The message names the danger so a reader can
    weigh it.
    """
    root = root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if status_of is None:
        def status_of(tid):
            from . import tasks
            row = tasks.get("task/%s" % tid)
            return (row or {}).get("status")
    out, scanned, unread = [], 0, []
    targets, missing = [], []
    for part in ("docs", "agents"):
        base = os.path.join(root, part)
        if not os.path.isdir(base):
            missing.append(part)
            continue
        walk_failed = []
        for dirpath, _dirs, names in os.walk(
                base, onerror=lambda e: walk_failed.append(e)):
            targets.extend(os.path.join(dirpath, n) for n in names
                           if n.endswith((".md", ".txt")))
        unread.extend("%s (%s)" % (part, e.__class__.__name__)
                      for e in walk_failed)
    for path in sorted(targets):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                body = fh.read()
        except OSError as e:
            unread.append("%s (%s)" % (os.path.relpath(path, root),
                                       e.__class__.__name__))
            continue
        # COUPLING IS BY REFERENCE DIRECTION, NOT BY LINE DISTANCE. A blind
        # window is wrong in both directions at once — too narrow and it misses
        # the construction, which wraps across lines; too wide and it marries a
        # citation to a promise made about something else entirely. Two rules
        # replace it, each answering a different half of the grammar:
        #   SAME SENTENCE — any promise clause, because one that names its own
        #   row ("until task/N lands") carries its referent with it.
        #   NEXT SENTENCE — only an ANAPHORIC clause ("until then"), whose
        #   "then" points BACK at the landing the previous sentence described.
        #   That is the shape the one real instance in this tree takes, and
        #   same-sentence coupling alone finds zero of it.
        # A PROVENANCE citation participates in neither: it points AT a record
        # rather than promising one, so the sentence after it is not about it.
        # That exclusion is what lets adjacency be safe.
        #
        # AND ADJACENCY IS SCOPED TO A BLOCK, which is the whole reason the
        # reach is safe to allow at all. "Next sentence" inside one paragraph
        # is a referent; "next sentence" across a code fence, a table, or into
        # the following bullet is just the next thing on the page.
        for text, offs in _prose_blocks(body):
            sents = _sentences(text)
            reported = set()                # (sentence_no, tid): once each
            for idx, (spos, sentence, sno) in enumerate(sents):
                # EVERY CONDITION IN THE CLAUSE IS REPORTED, not the first
                # one found: "task/1 is not yet available and until
                # task/2 lands" carries two promises about two rows.
                # A CLAUSE THAT NAMES ITS ROW SETTLES IT FIRST, so a bare
                # predicate beside it binds to what remains: "task/1 is not
                # yet available and until task/2 lands" is one promise about
                # each row, not two about both.
                phrases = sorted(_FUTURE_CONDITION.finditer(sentence),
                                 key=lambda m: not _TASK_CITE.search(m.group(0)))
                claimed = set()
                for phrase in phrases:
                    anaphoric = bool(
                        _ANAPHORIC_CONDITION.search(phrase.group(0)))
                    here, crossed = sentence, False
                    # A CITATION THAT IS ONLY PROVENANCE DOES NOT ANCHOR THE
                    # CLAUSE. "Until then, use the script recorded under task/M"
                    # points at M and promises nothing about it, so its "then"
                    # still reaches back to the previous sentence.
                    if idx and anaphoric \
                            and not _subject_cites(sentence) \
                            and _TASK_CITE.search(sents[idx - 1][1]):
                        here, crossed = sents[idx - 1][1], True
                    # THE PROVENANCE EXCLUSION APPLIES ONLY ACROSS A BOUNDARY, and
                    # that scope is the whole of its correctness. Within ONE
                    # sentence the promise is about the row that sentence names,
                    # however the citation is introduced — "see task/N once it
                    # lands" is still telling a reader to wait. It is only the
                    # REACH into a neighbouring sentence that a pointer must not
                    # license, because there the citation and the promise were
                    # written about different things and only adjacency suggests
                    # otherwise. And across that boundary it is applied per
                    # OCCURRENCE (`_subject_cites`): a row pointed at and then
                    # promised about in the same sentence is promised about.
                    # THE LOCATION IS THE PROMISE, NOT THE CITATION. When the two
                    # sit in different sentences the reader has to edit the stale
                    # clause, so that is the line worth printing; it is computed
                    # from the block's own offset map and is exact under wrapping
                    # and under repetition.
                    n = _lineno(offs, spos + phrase.start())
                    # ONE WARNING PER CITED ROW PER SENTENCE. The natural spelling
                    # of this construction names the row TWICE — "the door is
                    # task/2573 and until task/2573 lands, do it by hand" — and a
                    # per-MATCH loop reports the same rot twice for one sentence. A
                    # duplicated finding is not merely untidy: it inflates the
                    # count a reader uses to judge how bad the estate is.
                    cited = _subject_cites(here) if crossed else list(
                        dict.fromkeys(m.group(1)
                                      for m in _TASK_CITE.finditer(here)))
                    # WHICH ROW THE PROMISE IS ABOUT, AND A REFUSAL WHEN THAT
                    # CANNOT BE DECIDED. A clause that names its own row settles
                    # it: "until task/N lands" is about N whatever else the
                    # sentence mentions. A clause that says "it" does not, and a
                    # sentence naming TWO rows then offers no way to tell which
                    # one "it" is — "task/1 supersedes task/2, and once it lands
                    # the shim goes" is about task/1, and binding both warns about
                    # a row the sentence never promised anything about. Guessing
                    # here buys coverage with false warnings, which is the one
                    # trade this rung must not make, so it names nobody.
                    # THE REFUSAL IS FOR A PRONOUN. A predicate clause has no "it"
                    # to resolve: "task/1 and task/2 are not yet available" is
                    # about both rows, and naming neither would miss two.
                    named = _TASK_CITE.search(phrase.group(0))
                    if named:
                        cited = [named.group(1)]
                        claimed.add(named.group(1))
                    else:
                        cited = [tid for tid in cited if tid not in claimed]
                        if len(cited) > 1 and anaphoric:
                            continue
                    for tid in cited:
                        if (sno, tid) in reported:
                            continue
                        reported.add((sno, tid))
                        scanned += 1
                        try:
                            status = status_of(tid)
                        except Exception:      # noqa: BLE001 — a lookup is not a crash
                            status = None
                        rel = os.path.relpath(path, root)
                        if status is None:
                            out.append((WARN, "%s:%d cites task/%s as a future "
                                        "condition (%r) and this reader cannot "
                                        "resolve that row — UNKNOWN, not fine"
                                        % (rel, n, tid, phrase.group(0))))
                        elif status == "closed":
                            out.append((WARN, "%s:%d says %r about task/%s, which "
                                        "is CLOSED — the sentence now sends a "
                                        "reader down the manual path it describes "
                                        "as temporary, and that path is the "
                                        "riskier one"
                                        % (rel, n, phrase.group(0), tid)))
    # UNCERTAINTY IS REPORTED, NEVER ROUNDED TO CLEAN. A tree this rung could
    # not walk, a file it could not open, and a corpus with no targets at all
    # are three ways of measuring NOTHING — and a clean OK is what a reader
    # takes as "I looked and it is fine". An absent scan root is the loudest
    # of them: it usually means the rung is pointed somewhere wrong, and a
    # misaimed instrument reporting a clean estate is worse than one that
    # reports nothing.
    if missing:
        out.append((WARN, "doc-conditional scan found no %s under %s — this "
                    "rung measured nothing there and cannot report it clean"
                    % (" or ".join(missing), root)))
    if unread:
        out.append((WARN, "doc-conditional scan could not read %d path(s): %s "
                    "— UNKNOWN, not clean"
                    % (len(unread), ", ".join(sorted(unread)[:5]))))
    if out:
        return out
    if not targets:
        return [(WARN, "doc-conditional scan found no .md or .txt targets "
                 "under %s — nothing was measured" % root)]
    if not scanned:
        return [(OK, "no doc cites a task row as a future condition "
                 "(%d file(s) read)" % len(targets))]
    return [(OK, "%d doc task-conditional(s), every cited row still open"
             % scanned)]


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
    n = hooks.inject_covered(rows)
    msg = "inject coverage: %d of %d claude homes" % (n, len(rows))
    out = [(WARN, msg + " — `helm hooks install` closes the gap")] if n < len(rows) \
        else [(OK, msg)]
    # DRIFT GETS ITS OWN ROW, because coverage counts PRESENCE and a home
    # whose entry is merely an older rendering is present. Without this row
    # the estate would have no surface at all on which to learn that a
    # re-render is owed, so the widened count and this row are one change: the
    # failure mode a coverage-only reading produces is a deploy that reads as
    # an outage, or a drift nobody is told about.
    drift = hooks.drifted_rows(rows)
    if drift:
        out.append((WARN, "%d of %d claude homes run an OUT-OF-DATE hook "
                          "rendering — the entries work; `helm hooks install` "
                          "re-renders them" % (len(drift), len(rows))))
    # AND THE ROW FOR THE STATE NOTHING ELSE CAN SPEAK. A missing `helm-hook`
    # makes every hook in that home exit 127 before any line of it runs, which
    # the harness reads as ALLOW — in the shell's voice, never helm's. The
    # count above no longer admits such a home; this names it, because a
    # number that drops by one tells nobody which home or what to do.
    gone = hooks.wrapper_gone_message("claude home", rows)
    if gone:
        out.append((WARN, gone))
    return out


def check_guard_contract():
    """PER CONFIG — every seat AND every credential home: is every required
    guard actually in the file? (task/1006)

    HOMES ARE AUDITED BY THE SAME LIST, and their omission was this rung's own
    first defect: a home is a full launch surface too (the owner's `~/.claude`
    is one), so a guard missing there is exactly as unguarded as a seat. One
    function over both surfaces means a guard added to SPECS is asserted on
    every surface at once, rather than in two enumerations that drift.

    THE GAP THIS CLOSES WAS A COUNT THAT COULD NOT SEE ITS OWN BLIND SPOT.
    `hooks status` printed "10 of 10 seats covered (full hook contract)" on the
    same morning two seats carried no local-suite guard at all — because that
    guard was named in no spec list, so no census could count it, and the row
    it should have produced did not exist. The cure is not a better count: it is
    that the contract and the assertion read the SAME list, so a guard added
    there is asserted here for free.

    A missing guard is one LOUD row PER CONFIG naming it AND the guards, and a
    resolvable-but-absent executable is its own row — never a silent gap.
    Read-only: doctor names the defect, `helm hooks install` writes it.

    FINDINGS-ONLY, AND IT MAY PRINT OK UNDER NO CONDITION. unresolved, missing,
    stale, unreadable and unknown produce ROWS; fully resolved plus every config
    complete produces the EMPTY LIST. A specialized rung that also reports its
    own health spends a line of the operator's attention on the case that needed
    none, and — the reason it is a contract rather than a taste — an OK here is
    a POSITIVE claim about an estate this rung measures over a contract that
    `resolved_specs` may have shortened underneath it. Silence cannot make that
    claim. Broad doctor summaries still own the aggregate OK; this rung speaks
    only when something is wrong."""
    from . import hooks
    try:
        seats, unread = hooks.seat_gap_rows()
        homes = hooks.home_gap_rows()
        unresolved = hooks.unresolved_externals()
    except Exception as e:
        return [(WARN, "seat guard audit UNKNOWN (%s: %s)"
                       % (e.__class__.__name__, e))]
    # The host-level row first: a guard nothing can run is not a per-config gap,
    # and reporting it once above the per-config rows is what keeps it from
    # being restated N times as something `hooks install` could fix.
    out = [(WARN, msg) for _spec, _why, msg in unresolved]
    # THE LADDER BEFORE THE ENTRIES, on BOTH surfaces. A config can carry every
    # required guard and still run none of them: the commands all name one
    # `bin/helm-hook`, and if that file is not there each hook exits 127 and
    # the harness reads ALLOW. Rendered from `hooks.wrapper_gone_message` so
    # this rung, `hooks status` and the inject rung cannot word it three ways.
    for kind, rows in (("seat", seats), ("home", homes)):
        gone = hooks.wrapper_gone_message(kind, rows)
        if gone:
            out.append((WARN, gone))
        for r in rows:
            if r["missing"]:
                out.append((WARN, "%s %s is MISSING %d guard(s): %s (%s) — "
                                  "`helm hooks install` wires them"
                            % (kind, r["label"], len(r["missing"]),
                               ", ".join(r["missing"]), r["path"])))
            if r.get("memory") is False:
                out.append((WARN, hooks.memory_gap_message(kind, r)))
            if r["stale"]:
                out.append((WARN, "%s %s carries a STALE guard entry: %s (%s) "
                                  "— the config names it and this host cannot "
                                  "run it, so it is a claim, not coverage"
                            % (kind, r["label"], ", ".join(r["stale"]),
                               r["path"])))
    total = len(seats) + len(homes)
    if not total and not unread:
        return out
    clean = sum(1 for r in seats + homes if not r["missing"] and not r["stale"])
    msg = ("guard contract: %d of %d config(s) carry every required guard "
           "(%d seat(s), %d home(s))"
           % (clean, total, len(seats), len(homes)))
    # `unread` is the census's own completeness — "N of N" beside an unreadable
    # subtree is "healthy among the seats the walk could read", never healthy.
    if unread:
        out.append((WARN, msg + " — but %d seat root(s) were UNREADABLE (%s), "
                           "so both counts are a FLOOR"
                    % (len(unread), "; ".join(p for p, _w in unread))))
    elif unresolved:
        # NEVER OK WHILE A REQUIRED GUARD IS UNRESOLVABLE. Every config can be
        # complete over the guards this host CAN install and the estate still be
        # unguarded — `resolved_specs` dropped the unresolvable one, so the
        # count above is taken over a SHORTENED contract. Emitting OK beside
        # that is the install-reports-full defect wearing a doctor hat: a clean
        # word earned by not asking the question.
        out.append((WARN, msg + " — but %d required guard(s) (%s) resolve to "
                           "NOTHING here, so this is measured against a "
                           "SHORTENED contract and no config is fully guarded"
                    % (len(unresolved),
                       ", ".join(s["name"] for s, _w, _m in unresolved))))
    return out


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


# The engines the local-suite guard wraps, and the ones whose import at
# interpreter start is the whole cost: importing `unittest` pulls
# `unittest.suite`, and `doctest` pulls `pdb` behind it.
_STARTUP_ENGINES = ("unittest", "doctest", "pdb")

# THE GUARD'S OWN EXEMPTIONS, SCRUBBED FROM THE CHILD. A routed run sets these,
# and the guard installs NO door when it sees one — correctly, since helm runs
# suites in-process inside a gate. A probe that inherited them would therefore
# read a healthy armed guard as absent, which is the one wrong answer this rung
# must never give. The child is asked the unexempted question.
_STARTUP_EXEMPTIONS = ("FAB_ID", "HELM_GATE_SUITE_CAP", "FAB_ALLOW_LOCAL_SUITE")

# ONE CHILD ANSWERS BOTH HALVES, and the order inside it is load bearing: the
# engines are read out of `sys.modules` BEFORE this source imports anything
# that could put them there, and only then is a one-case suite offered to the
# doors. `json`, `os` and `site` import none of the three.
_STARTUP_CHILD = """\
import json, os, site, sys
eager = [m for m in %r if m in sys.modules]
try:
    where = site.getusersitepackages()
except Exception:
    where = ""
refused = False
try:
    import unittest

    class _One(unittest.TestCase):
        def runTest(self):
            pass

    try:
        unittest.TestSuite([_One()]).run(unittest.TestResult())
    except SystemExit:
        refused = True
except Exception:
    pass
sys.stdout.write(json.dumps({"eager": eager, "user_site": where,
                             "refused": refused, "pid": os.getpid()}))
"""


def _startup_probe(exe=None, repeats=3, run=None):
    """-> a measurement of what a FRESH interpreter costs and carries.

    MEASURE THE EFFECT, NEVER THE FILE. The artifact that decides this lives
    outside the repository, so any check that reads its bytes is a proxy for a
    property and drifts the moment the bytes move. The observable is: which
    test engines a child carries the instant it starts, whether that child is
    still refused a suite, and what the site stage costs.

    THE SITE STAGE IS A DIFFERENCE, not a stopwatch reading. A bare start
    measures the interpreter plus the site stage; the same start with the site
    stage skipped measures the interpreter alone. The gap between them is the
    number a hook process pays on every tool call. The MINIMUM of the repeats
    is taken on each arm, because this box runs hot and a mean there measures
    the load rather than the change.

    Returns ``{"ok": False, "why": ...}`` for every unreadable world — no
    interpreter, a refused spawn, an unparseable answer — so the caller can
    report UNKNOWN. It never guesses a half-measurement into a clean bill.
    """
    import json
    import subprocess as sp

    runner = run or sp.run
    exe = exe or sys.executable
    if not exe:
        return {"ok": False, "why": "this process reports no interpreter path, "
                                    "so no child could be started"}
    env = {k: v for k, v in os.environ.items() if k not in _STARTUP_EXEMPTIONS}

    def spawn(argv):
        return runner([exe] + argv, capture_output=True, text=True,
                      timeout=60, env=env)

    try:
        got = spawn(["-c", _STARTUP_CHILD % (_STARTUP_ENGINES,)])
    except Exception as e:                                     # noqa: BLE001
        return {"ok": False, "why": "the probe child did not run (%s: %s)"
                % (e.__class__.__name__, e)}
    try:
        # the LAST line: a chatty site stage writes its banner first, and a
        # banner is a reason to keep measuring rather than to report nothing
        said = json.loads((got.stdout or "").strip().splitlines()[-1])
        row = {"ok": True, "eager": list(said["eager"]),
               "user_site": said["user_site"], "refused": bool(said["refused"]),
               "pid": int(said["pid"])}
    except Exception as e:                                     # noqa: BLE001
        return {"ok": False, "why": "the probe child answered unreadably "
                                    "(%s: %s)" % (e.__class__.__name__, e)}
    best = []
    for flag in ([], ["-S"]):
        seen = []
        for _ in range(repeats):
            began = time.perf_counter()
            try:
                spawn(flag + ["-c", ""])
            except Exception as e:                             # noqa: BLE001
                return {"ok": False, "why": "a timing child did not run (%s: %s)"
                        % (e.__class__.__name__, e)}
            seen.append((time.perf_counter() - began) * 1000.0)
        best.append(min(seen))
    row["site_ms"] = max(0.0, best[0] - best[1])
    return row


def check_startup_doors(probe=None):
    """The half of the hook budget that lives OUTSIDE this repository.

    A per-tool-call hook is a fresh interpreter, so its floor is whatever the
    site stage costs — and on an agents-only host the site stage carries a
    machine-local local-suite guard. That guard has to hold two properties at
    once: install its refusal doors from a `sys.meta_path` finder, so a process
    that imports no test engine pays nothing, AND still refuse a suite in any
    process that does. helm ships neither half, and the arms in this tree stay
    green whether or not they hold, so without this rung a rebuilt box loses
    the hook budget with nothing on any surface saying why.

    THREE STATES, AND UNKNOWN IS NOT ONE OF THE GOOD ONES. Lazy and armed is
    OK; eager is a WARN carrying the measured cost, the artifact and the cure;
    a measurement that could not be taken is a WARN that says UNKNOWN, because
    silence about an unasked question is indistinguishable from health.

    AND AN ABSENT GUARD MEASURES EXACTLY LIKE A LAZY ONE on the import axis: a
    file that was never installed imports nothing either. Only the refusal
    separates them, so the probe offers the child a one-case suite and the OK
    row is spent only when the child was refused. What that proves is bounded,
    and the row says so: the suite door in a child of THIS interpreter, not the
    guard's pytest or doctest doors and not its loader arms.
    """
    try:
        row = (probe or _startup_probe)()
    except Exception as e:                                     # noqa: BLE001
        row = {"ok": False, "why": "%s: %s" % (e.__class__.__name__, e)}
    if not row.get("ok"):
        return [(WARN, "interpreter startup: UNKNOWN (%s) — no child "
                       "interpreter could be measured, so BOTH halves are "
                       "unread: whether the site stage imports a test engine, "
                       "and whether a local suite is still refused. An "
                       "unmeasured guard is not a clean one."
                 % row.get("why", "no reason recorded"))]
    where = row.get("user_site") or ("this interpreter's own per-version user "
                                     "site directory")
    if row["eager"]:
        return [(WARN, "interpreter startup: the site stage imports %s at EVERY "
                       "interpreter start and costs %.0f ms, which every hook "
                       "process on this box pays on every tool call. The "
                       "file that decides this here is the machine-local "
                       "local-suite guard, a usercustomize.py in %s: it must "
                       "install its refusal doors from a sys.meta_path finder "
                       "rather than import the test engines at module scope. "
                       "Restore the finder-based file there, or re-run its "
                       "installer, then re-measure — and note the site stage is "
                       "measured while its author is not, so any other site "
                       "file on the path can produce this reading too. Its "
                       "teeth are %s."
                 % (", ".join(row["eager"]), row["site_ms"], where,
                    "intact: a one-case suite was REFUSED in that child"
                    if row["refused"] else
                    "GONE TOO: nothing refused a one-case suite in that child"))]
    if not row["refused"]:
        return [(WARN, "interpreter startup: no test engine is imported at "
                       "startup (site stage %.0f ms), but NOTHING REFUSED a "
                       "one-case suite in that child — the machine-local "
                       "local-suite guard (a usercustomize.py in %s) is ABSENT "
                       "or DISARMED on this interpreter, and an absent guard "
                       "measures exactly like a lazy one on the import axis. "
                       "Restore the finder-based file there; a local suite run "
                       "is unguarded until it is back."
                 % (row["site_ms"], where))]
    return [(OK, "interpreter startup: site stage %.0f ms and no test engine "
                 "imported (unittest, doctest and pdb are all absent from a "
                 "fresh child), and that same child was still REFUSED a "
                 "one-case suite — the machine-local local-suite guard installs "
                 "its doors lazily and keeps its teeth. Measured by starting a "
                 "child, not by reading the guard's source: its pytest and "
                 "doctest doors and its loader arms are NOT probed here."
             % row["site_ms"])]


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


def check_seat_memory_ceilings(census=None, root=None, proc=None):
    """Warns ONLY when the throttle observation cannot be obtained.

    THIS CHECK DELIBERATELY CANNOT FAIL ON A COUNTER. `memory.events` is
    cumulative: an old throttle persists in it for a whole healthy lifetime,
    so warning because the number is nonzero converts HISTORY into a
    present-action signal — which is the inversion the module it calls exists
    to stop. A zero is not health either; it is a counter reading zero.

    So doctor says one thing here: whether the observation could be taken at
    all. What the counters SAY belongs beside the census row they describe
    (`helm fleet`). Whether a seat is throttled NOW is a different read —
    seatceiling.fleet_pressure, built from a stalled process or a counter
    RISING across a sampled window, never from the counter's size — and it has
    its own destinations: the mark on the seat's `helm chat seats` row, the
    badge on the web roster, the scratch reaper's third plane (`helm scratch
    gc`), and one wake per spell to the seat's lead and the integrator.

    `census`, `root` and `proc` are injectable so a test can OWN this input
    rather than consume live host pressure. BUT THEY DO NOT ISOLATE THE FULL
    DOCTOR, and saying so is the point of this paragraph: the CHECKS loop
    resolves each name and calls it with NO arguments, so `helm doctor` still
    reads the live census. A seat that exits between the census bracket and
    the cgroup read still produces a WARN row here, and a full-doctor fixture
    asserting a green estate is still coupled to that race. Isolating THAT
    needs the fixture to stub the census; this signature is what makes
    stubbing it possible, not a substitute for doing so.

    IT TAKES ALL THREE TO ISOLATE AN ARM. A root alone leaves the fixture
    deriving the cgroup PATH and the GENERATION from the live host, so the
    answer stays partly a property of the box that ran it — and a process in
    the cgroup root, one several levels down, and one whose chain has a level
    with no counter interface are all ordinary, so a determinate control has
    to supply which of them it is asking about.
    """
    try:
        from . import seatceiling, seats_common, session
    except Exception as e:
        return [(WARN, "seat throttle observation unavailable (%s: %s)"
                       % (e.__class__.__name__, e))]
    if census is None:
        try:
            census = session._proc_claude_census()
        except Exception as e:
            return [(WARN, "the seat census failed (%s: %s), so no throttle "
                           "observation could be taken"
                           % (e.__class__.__name__, e))]
    if census.get("listing_failed"):
        return [(WARN, "the /proc enumeration failed, so no throttle "
                       "observation could be taken — zero rows is a failed "
                       "probe, not a proven-empty estate")]
    rows = census.get("rows") or ()
    where = {}
    if root is not None:
        where["root"] = root
    if proc is not None:
        where["proc"] = proc
    blind = []
    for r in rows:
        try:
            obs = seatceiling.observe(r["pid"], start=r.get("start"), **where)
        except Exception as e:
            blind.append((r, "%s: %s" % (e.__class__.__name__, e)))
            continue
        if not seatceiling.acquisition_complete(obs):
            # ONE TYPED QUESTION, NOT THREE PROSE ONES. `acquisition_complete` is
            # true when the bracket held at BOTH ends and every level asked
            # about answered — so a reused pid, a process that migrated
            # mid-read, a level that vanished and a missing bracket all reach
            # this branch without doctor enumerating them. Reading a sentence
            # to decide is what let a row with no proven generation through.
            blind.append((r, obs.bracket.why
                          or "%d cgroup level(s) did not answer"
                             % len(seatceiling.unobtainable(obs))))
    if not rows:
        if census.get("census_partial"):
            return [(WARN, "the census could not certify completeness, so an "
                           "empty result does not show an empty estate")]
        return [(OK, "no seat processes on this box")]
    # A PARTIAL CENSUS QUALIFIES EVERY ANSWER DRAWN FROM IT, not only the
    # empty one. The producer states that a partial population is a FLOOR
    # rather than a certified total, so rows that WERE observed are still an
    # unknown fraction of the estate — reading the flag in the empty branch
    # alone let a good row answer for a population nobody could count. ONE
    # census-wide line, not a repetition on every healthy row.
    out = []
    if census.get("census_partial"):
        out.append((WARN, "the census could not certify completeness, so the "
                          "seats below are a FLOOR and not the estate — a "
                          "seat it could not classify is not observed here"))
    for r, why in blind[:5]:
        seat = seats_common._seat_label(
            (r.get("environ") or {}).get("HELM_CHAT_NAME") or "-")
        out.append((WARN, "seat %s (pid %d): throttle observation could not be "
                          "taken — %s" % (seat, r["pid"], why)))
    if len(blind) > 5:
        out.append((WARN, "%d further seat(s) could not be observed"
                          % (len(blind) - 5)))
    # KEYED ON BLIND ROWS, NOT ON `out`. The census-wide qualifier above is
    # about the POPULATION; it does not stop the seats that WERE observed
    # from being reported as observed, and suppressing the count because a
    # warning exists would hide the one number this check is for.
    if not blind:
        out.append((OK, "%d seat(s) observable for throttle counters; this "
                        "check does not judge them — it reports only that the "
                        "observation could be taken" % len(rows)))
    return out


def check_env():
    """HELM_*/MELD_* overrides in effect."""
    out = [(OK, "env override: %s=%s" % (var, os.environ[var]))
           for var in ("HELM_HOME", "MELD_HOME") if os.environ.get(var)]
    return out or [(OK, "no env overrides — home resolves to ~/.helm")]


def check_local_names():
    """This host's local-names file configures what it says it does.

    EVERY FAULT IN IT IS SILENT WHERE IT IS READ. A key the reader cannot use
    configures nothing, so the behaviour it names quietly takes its neutral
    default: a predecessor's variables stop being honoured, a pool row goes by
    a name no live config carries. This rung is where that is said. An absent
    file is the fresh-clone case and owes nothing."""
    from . import localnames
    return [(WARN, "local names: %s — that setting takes its neutral default "
                   "until it is fixed" % why) for why in localnames.problems()]


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


# ---------------------------------------------------------------------------
# THE AUTO-MEMORY BASE: does Claude Code still honour the variable?
#
# `helm hooks install` renders hooks.MEMORY_BASE_ENV into every credential
# home whose `projects` is a symlink, and check_guard_contract proves the KEY
# is in each settings file. That proves nothing about the harness. The
# variable is UNDOCUMENTED: a Claude Code release that drops or renames it
# leaves every key in place, every doctor row green, and every seat stalled on
# a permission prompt at its next memory write. Two probes close that, at two
# prices:
#
#   STATIC  (every doctor pass, milliseconds, no model): the installed program
#           still contains the name. Absent is a FAIL, because a program that
#           does not contain the name cannot read it. Present is weaker: it
#           does not prove the memory-base function still reads it.
#   LIVE    (`helm doctor --probe-memory`, one haiku call): one headless memory
#           write in a scratch tree, with the variable naming a base whose path
#           has a `.claude` segment, exactly as a seat's does. The note must
#           land under that base. Run it after every Claude Code upgrade; the
#           result is recorded per version, and the static row reports it.
#
# Neither fires when no credential home links its projects tree: such a home
# needs no base, so nothing depends on the variable.
MEMORY_PROBE_TIMEOUT = 300
_WRAPPER_MAX = 1 << 16      # a launcher script, not the program it runs
_PROGRAM_MAX = 1 << 30      # the bound on the static read
_CLAUDE_VERSIONS = os.path.join("~", ".local", "share", "claude", "versions")
_VERSIONED_PROGRAM = re.compile(r"/claude/versions/(\d+(?:\.\d+)+)/?$")
# The probe's memory write is the only thing it lets the model do: Bash cannot
# write around the check, and only the flag settings apply, so the home's hooks
# never fire. No autoMemoryDirectory: it outranks the variable in Claude Code's
# resolver, so a probe that set it would test nothing.
_PROBE_FLAGS = ("--permission-mode", "auto", "--permission-prompts", "none",
                "--disallowedTools", "Bash", "--model", "haiku",
                "--no-session-persistence", "--setting-sources", "project",
                "--output-format", "json")


# THE HOMES THE PROBE NEVER BORROWS. The probe runs `claude -p` on a home it
# reads in place, and Claude Code refreshes that home's token when it needs to.
# A home that is one seat's own login must not be borrowed: the probe would
# rotate the credential under a live seat that is not the caller. No helm
# registry records seat-reserved homes (accounts.json, the seat roster, cred
# and homes hold no such field), and a home's label is its owner's login
# folded, so the operator names them in this file under the helm home and the
# source names none.
# A caller whose own CLAUDE_CONFIG_DIR is one of these still probes on it: it
# is that caller's home, and nothing is borrowed.
PROBE_RESERVED_CONFIG = "probe-reserved-homes.json"


def probe_reserved_homes():
    """({label: why}, None), or (None, why) when the file does not read.

    Absent reserves nothing. Unreadable is NOT absent: it cannot say which
    homes are reserved, so the caller refuses every home it would borrow."""
    _path, table, why = home.global_json(PROBE_RESERVED_CONFIG)
    if why:
        return None, why
    return {str(k): str(v) for k, v in (table or {}).items()}, None


def _memory_homes():
    """[(label, realpath)] of the credential homes that need the memory base."""
    from . import hooks
    return [(n, p) for n, p in hooks.claude_homes() if hooks.memory_base(p)]


def _claude_version(run):
    """The semver `claude --version` prints, or None."""
    import subprocess as _sp
    try:
        out = run(["claude", "--version"], capture_output=True, text=True,
                  timeout=15).stdout or ""
    except (_sp.TimeoutExpired, OSError):
        return None
    m = re.search(r"\d+\.\d+\.\d+", out)
    return m.group(0) if m else None


def claude_program(which=None, run=None, versions_dir=None):
    """-> (path, version, why): the file `claude` on PATH actually runs.

    A versioned install (`.../claude/versions/<semver>`) names itself. A small
    `#!` launcher (this estate's slice shim) runs a versioned program, so the
    version `claude --version` reports picks it out of the versions dir. Any
    other file is the program itself. `path` None means it could not be found,
    and `why` says why — never a guess."""
    import shutil as _sh
    import subprocess as _sp
    run = run or _sp.run
    found = (which or _sh.which)("claude")
    if not found:
        return None, None, "`claude` is not on PATH"
    real = os.path.realpath(found)
    m = _VERSIONED_PROGRAM.search(real)
    if m:
        return real, m.group(1), None
    try:
        with open(real, "rb") as f:
            head = f.read(2)
        size = os.path.getsize(real)
    except OSError as e:
        return None, None, "%s cannot be read (%s)" % (real, e.strerror or e)
    ver = _claude_version(run)
    if head != b"#!" or size > _WRAPPER_MAX:
        return real, ver, None
    if not ver:
        return None, None, ("%s is a launcher script and `claude --version` "
                            "named no version" % real)
    prog = os.path.join(os.path.expanduser(versions_dir or _CLAUDE_VERSIONS), ver)
    if os.path.isfile(prog):
        return prog, ver, None
    return None, ver, ("%s is a launcher script and the program it runs for %s "
                       "is not at %s" % (real, ver, prog))


def _names_memory_base(path):
    """Does the file at `path` contain the variable's name? Raises OSError when
    it cannot be read, so unreadable never reads as absent."""
    import errno as _errno
    import mmap
    from .hooks import MEMORY_BASE_ENV
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        if size > _PROGRAM_MAX:
            raise OSError(_errno.EFBIG, "larger than the %d-byte read bound"
                          % _PROGRAM_MAX)
        if not size:
            return False
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            return m.find(MEMORY_BASE_ENV.encode()) != -1


def _memory_probe_path():
    return os.path.join(home.global_dir(), ".state", "memory-probe.json")


def _memory_probe_records():
    """({version: record}, why): the live probe's results. An absent file is no
    record; a file that cannot be parsed is `why`, never an empty history."""
    path = _memory_probe_path()
    if not os.path.lexists(path):
        return {}, None
    got = pk.read_json(path)
    if not isinstance(got, dict):
        return {}, "the live-probe record %s is UNREADABLE" % path
    return got, None


def _probe_note(version):
    """-> (level, sentence) for the live probe's standing on `version`."""
    recs, why = _memory_probe_records()
    if why:
        return OK, why
    rec = recs.get(version) if version else None
    if not isinstance(rec, dict):
        return OK, ("the live probe has not run on %s — run `helm doctor "
                    "--probe-memory` after every Claude Code upgrade"
                    % (version or "this version"))
    if rec.get("ok") is True:
        return OK, "the live probe passed on %s at %s" % (version, rec.get("at"))
    return FAIL, ("the live probe FAILED on %s at %s: %s"
                  % (version, rec.get("at"), rec.get("detail")))


def check_memory_base_honoured():
    """STATIC: the installed Claude Code still contains the memory-base variable.

    Three outcomes and they never share a level: contains it -> OK (with the
    live probe's standing on this version, which a recorded failure turns
    FAIL); does not contain it -> FAIL naming the variable and the version;
    could not locate or read the program -> WARN that says UNKNOWN and why. No
    home that links its projects tree -> no row: nothing depends on it."""
    from .hooks import MEMORY_BASE_ENV
    try:
        homes = _memory_homes()
    except Exception as e:
        return [(WARN, "memory base: credential home census UNKNOWN (%s: %s)"
                       % (e.__class__.__name__, e))]
    if not homes:
        return []
    need = ("%d credential home(s) whose projects is a link depend on it"
            % len(homes))
    prog, ver, why = claude_program()
    shown = ver or "(version unknown)"
    if not prog:
        return [(WARN, "memory base: whether Claude Code still honours %s is "
                       "UNKNOWN — %s; %s" % (MEMORY_BASE_ENV, why, need))]
    try:
        has = _names_memory_base(prog)
    except OSError as e:
        return [(WARN, "memory base: whether Claude Code %s still honours %s is "
                       "UNKNOWN — %s cannot be read (%s); %s"
                 % (shown, MEMORY_BASE_ENV, prog, e.strerror or e, need))]
    if not has:
        return [(FAIL, "memory base: Claude Code %s (%s) no longer contains %s, "
                       "so it cannot honour it: every auto-memory write on the "
                       "%d credential home(s) whose projects is a link now "
                       "stops on a permission prompt. Find what replaced the "
                       "variable before any seat restarts on this version"
                 % (shown, prog, MEMORY_BASE_ENV, len(homes)))]
    level, note = _probe_note(ver)
    return [(level, "memory base: Claude Code %s still contains %s (%s); %s"
             % (shown, MEMORY_BASE_ENV, need, note))]


def memory_relaunch(row):
    """The one relaunch line for a session that predates its home's memory
    base: the seat's own launch door on its own home, resuming its session."""
    sid = row.get("session") or "<its session id>"
    who = row.get("seat")
    if who:
        return ("/exit in its pane, then `helm launch --seat %s --home %s -- "
                "--resume %s` in the same pane" % (who, row["home"], sid))
    return ("/exit in its pane, then `CLAUDE_CONFIG_DIR=%s claude --resume %s`"
            % (row["root"], sid))


def check_memory_base_sessions():
    """RUNNING sessions that predate their home's memory-base render.

    check_memory_base_honoured proves the installed program reads the
    variable and the home rung proves the key is in settings. Neither can see
    a session that was ALREADY RUNNING when the key arrived: Claude Code
    resolves the memory dir once, at session start, so such a session keeps
    stopping on a permission prompt at every memory write while every file
    reads green (seatstale.memory_base_state holds the measurement).

    A NAMED FAIL, because the cost is certain and the remedy is one relaunch.
    The relaunch is the owner's or the integrator's: helm never types into a
    pane, so the row names the exact line rather than doing it."""
    from . import seatstale
    from .hooks import MEMORY_BASE_ENV
    st = seatstale.memory_base_state()
    if not st["read"]:
        return [(WARN, "memory base sessions: %s — whether any running session "
                       "predates its home's %s is UNKNOWN"
                 % (st["why"], MEMORY_BASE_ENV))]
    out = []
    for row in st["predates"]:
        out.append((FAIL, "memory base: %s (pid %s, home %s) STARTED BEFORE ITS "
                          "HOME CARRIED %s — Claude Code fixes the auto-memory "
                          "dir when a session starts, so this session still "
                          "writes memory through the linked path and EVERY "
                          "memory write it makes stops on a permission prompt "
                          "until it is relaunched. The owner or the integrator "
                          "relaunches it with --resume: %s. helm never types "
                          "into a pane"
                    % (("seat %s" % row["seat"]) if row["seat"] else
                       "an unnamed session", row["pid"], row["home"],
                       MEMORY_BASE_ENV, memory_relaunch(row))))
    for row in st["blind"]:
        out.append((WARN, "memory base: %s (pid %s, home %s): whether it started "
                          "with %s could not be decided — %s"
                    % (("seat %s" % row["seat"]) if row["seat"] else
                       "an unnamed session", row["pid"], row["home"],
                       MEMORY_BASE_ENV, row["why"])))
    if st["measured"] and not st["predates"]:
        out.append((OK, "memory base: %d running session(s) on a linked home "
                        "started with %s in force"
                    % (st["measured"], MEMORY_BASE_ENV)))
    return out


def _probe_env(config_dir, base):
    """The probe's environment: the caller's, minus every Claude, Anthropic and
    seat-identity variable (a child stamp, a proxy base URL, an inherited
    memory override or the seat's own memory base would each change the
    answer), plus the home to read in place and the base under test."""
    from .hooks import MEMORY_BASE_ENV
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLAUDE", "ANTHROPIC", "HELM_CHAT"))}
    env["CLAUDE_CONFIG_DIR"] = config_dir
    env[MEMORY_BASE_ENV] = base
    return env


def _holds(path, nonce):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return nonce in f.read(4096)
    except OSError:
        return False


def _probe_home(homes, now_ms=None):
    """-> (label, config_dir, None) for the home the live probe runs on, or
    (None, None, why) naming why each candidate was refused.

    The caller's own CLAUDE_CONFIG_DIR wins when it is a linked home with
    credentials: it is that caller's home and already live. Otherwise the
    first home by name that has credentials, is not reserved, and whose
    access token has not expired. The expiry comes from seat_rehome's
    secret-free reader over cred's own readers; only a timestamp leaves it."""
    from . import cred
    from .seat_rehome import _home_token_facts, _utc
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    here = os.path.realpath(os.environ.get("CLAUDE_CONFIG_DIR") or os.sep)
    has = [(n, p) for n, p in homes
           if os.path.isfile(os.path.join(p, cred.AUTH_JSON))]
    for n, p in has:
        if p == here:
            return n, p, None
    reserved, unread = probe_reserved_homes()
    skipped = []
    for n, p in homes:
        facts, err = ((None, "no %s" % cred.AUTH_JSON)
                      if (n, p) not in has else _home_token_facts(p))
        exp = (facts or {}).get("expires_at")
        why = (err or unread or reserved.get(n)
               or (exp is None and "no readable access-token expiry")
               or (exp <= now_ms and "access token expired at %s" % _utc(exp)))
        if not why:
            return n, p, None
        skipped.append("%s: %s" % (n, why))
    return None, None, ("no linked home qualifies to run it (%s)"
                        % "; ".join(skipped))


def probe_memory_live(run=None, program=None):
    """LIVE: one headless memory write, and whether it landed under the base.

    -> (level, message, version). The scratch tree is `<tmp>/base/.claude/
    projects` (the `.claude` segment is the one that made the seats prompt),
    and a work dir with its own `.git`, so the project slug is unique. The home
    is read in place through CLAUDE_CONFIG_DIR; this code never opens its
    credentials. The scratch tree is deleted on every path.

    The note under the base -> OK. A refused write, or a note that landed
    through the home's link instead -> FAIL: the variable was not honoured.
    No claude, no qualifying home, a timeout, no JSON result, or no write at
    all -> WARN that says UNKNOWN: none of those says anything about the
    variable.

    Every outcome after a home is chosen names that home (_probe_home picks
    it). When none qualifies the UNKNOWN names why each was refused, and
    nothing is spawned, not even `claude --version`."""
    import subprocess as _sp
    from .hooks import MEMORY_BASE_ENV
    run = run or _sp.run
    unknown = ("live memory probe: whether Claude Code honours %s is UNKNOWN — "
               % MEMORY_BASE_ENV)
    homes = _memory_homes()
    if not homes:
        return OK, ("live memory probe: no credential home links its projects "
                    "tree, so nothing depends on %s; nothing to probe"
                    % MEMORY_BASE_ENV), None
    label, cdir, why = _probe_home(homes)
    if not cdir:
        return WARN, unknown + why, (program or (None, None, None))[1]
    prog, ver, pwhy = program or claude_program(run=run)
    level, msg = _probe_on(cdir, prog, ver, pwhy, run, unknown)
    return level, "%s; home %s, read in place" % (msg, label), ver


def _probe_on(cdir, prog, ver, why, run, unknown):
    """-> (level, message) for one probe on home `cdir`; the caller names the
    home on every outcome, so no branch here can leave it out."""
    import glob as _glob
    import json as _json
    import secrets
    import shutil as _sh
    import subprocess as _sp
    import tempfile
    from .hooks import MEMORY_BASE_ENV
    if not prog:
        return WARN, unknown + why
    scratch = tempfile.mkdtemp(prefix="helm-memprobe-")
    try:
        base = os.path.join(scratch, "base", ".claude")
        work = os.path.join(scratch, "work")
        os.makedirs(os.path.join(base, "projects"))
        os.makedirs(os.path.join(work, ".git"))
        flags = os.path.join(scratch, "settings.json")
        pk.atomic_write(flags, _json.dumps({"autoMemoryEnabled": True}))
        nonce = secrets.token_hex(8)
        name = "helm-memory-probe-%s.md" % nonce
        prompt = ("Save one auto-memory note. Use the Write tool to create a "
                  "file named %s directly inside your auto-memory directory "
                  "(the memory directory your instructions name), containing "
                  "exactly the line: %s. Write nothing else anywhere. Then "
                  "reply with only the absolute path you wrote." % (name, nonce))
        argv = [prog, "-p", prompt] + list(_PROBE_FLAGS) + ["--settings", flags]
        try:
            p = run(argv, cwd=work, env=_probe_env(cdir, base), capture_output=True,
                    text=True, timeout=MEMORY_PROBE_TIMEOUT)
        except (_sp.TimeoutExpired, OSError) as e:
            return WARN, unknown + ("claude -p did not finish (%s)"
                                    % e.__class__.__name__)
        try:
            out = _json.loads(p.stdout or "")
        except ValueError:
            out = None
        if not isinstance(out, dict):
            tail = ((p.stderr or "").strip().splitlines() or [""])[-1][:160]
            return WARN, unknown + ("claude -p exited %s with no JSON result (%s)"
                                    % (p.returncode, tail or "no stderr"))
        cost = out.get("total_cost_usd")
        spent = ("; probe cost $%.4f" % cost
                 if isinstance(cost, (int, float)) else "")
        mem = os.path.join("*", "memory", name)
        if any(_holds(f, nonce) for f in
               _glob.glob(os.path.join(base, "projects", mem))):
            return OK, ("live memory probe: Claude Code %s put the memory write "
                        "under %s, so it honours the variable%s"
                        % (ver, MEMORY_BASE_ENV, spent))
        slug = os.path.join(cdir, "projects", "*%s*" % os.path.basename(scratch))
        stray = _glob.glob(os.path.join(slug, "memory", name))
        denied = [(d.get("tool_input") or {}).get("file_path") for d in
                  out.get("permission_denials") or [] if isinstance(d, dict)]
        if denied or stray:
            left = sorted(_glob.glob(slug))
            return FAIL, ("live memory probe: Claude Code %s did NOT honour %s — "
                          "the memory write went to %s and %s, so every seat on "
                          "a linked home stops on a permission prompt at its "
                          "next memory write%s%s"
                          % (ver, MEMORY_BASE_ENV, (stray or denied)[0],
                             "LANDED through the link" if stray else "was refused",
                             "; Claude Code left %s in the shared tree"
                             % ", ".join(left) if left else "", spent))
        return WARN, unknown + ("the model made no memory write the probe could "
                                "see (%s)%s" % (str(out.get("result"))[:160],
                                                spent))
    finally:
        _sh.rmtree(scratch, ignore_errors=True)


def record_memory_probe(level, message, version):
    """Record an OK or FAIL live probe under its version, so the static row
    reports it on every later doctor pass. UNKNOWN is not a result and is not
    recorded; neither is a probe whose version is unknown."""
    import json as _json
    if level not in (OK, FAIL) or not version:
        return
    recs, why = _memory_probe_records()
    recs = {} if why else recs
    recs[version] = {"ok": level == OK,
                     "detail": message[:400],  # noqa: SILENT_CAP — a summary of a row the report printed whole
                     "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    pk.atomic_write(_memory_probe_path(),
                    _json.dumps(recs, indent=1, sort_keys=True) + "\n")


def _signer_unverified_row(cores):
    """-> (level, row). ONE FLEET FACT, NOT A PER-SEAT FAULT.

    Every seat runs its own doctor, so a row phrased about "your signer" is
    the SAME condition rendered N times and reads as N regressions. The
    condition is a property of ONE binary, so the row names that binary, how
    far it reaches, and what clears it — and a reader who meets it on the
    tenth pane can recognise the row they already read on the first.
    """
    from . import cell as _cell, chat
    path = _cell.bin_path()
    resolved = _cell._resolve(path) if path else None
    where = "the configured signer"
    when = ""
    if resolved:
        # THE PATH IS NAMED because an operator cannot act on a condition
        # whose subject is anonymous, and it is laundered on the way out
        # because it arrives from an operator-set variable like every other
        # signer string this module renders.
        where = chat._safe_reason(resolved)
        try:
            # MODIFICATION TIME, SAID AS MODIFICATION TIME. Nothing here
            # measures build provenance: a copy, a restore or a touch moves
            # mtime without rebuilding anything, so calling it "built" would
            # attribute an origin this row never established.
            when = " (file mtime %s)" % time.strftime(
                "%Y-%m-%d", time.localtime(os.path.getmtime(resolved)))
        except OSError:
            when = ""
    reach = _signer_reach_phrase(_cell, resolved)

    declared, source = _cell.unaudited_declared()
    # THE SELF-REPORT IS ABOUT THE CONFIGURED BINARY, which is the one helm
    # would exec — not a statement that a signed turn ran through it just now.
    head = ("the configured chat signer reports the unaudited PQ fallback — "
            "%s%s %s" % (where, when, chat._safe_reason(cores["reason"])))
    tail = " ONE BINARY, not a per-seat fault: %s." % reach
    if declared is None:
        return (WARN, head + tail +
                " Whether an operator declared this posture could not be read,"
                " so it is reported without being called either way.")
    if declared:
        # ONLY THE SOURCE IS ATTRIBUTED. This check establishes THAT a
        # declaration exists and WHERE — never what it says, which it has not
        # read and which a bare env var does not have at all. So the row sends
        # the reader to the source, and helm's own pointer is offered
        # separately and marked as helm's.
        return (WARN, head + tail +
                " DECLARED in %s; read it for scope and reversal."
                " helm's pointer: task/2342 delivers an audited signer."
                % chat._safe_reason(source or "an unnamed source"))
    return (FAIL, head + tail +
            " NOTHING DECLARES this posture — dregg only serves unverified"
            " through an operator's hatch, so an undeclared one is a signer"
            " nobody chose. helm's pointer: task/2342 delivers an audited"
            " signer.")


def _signer_reach_phrase(_cell, resolved):
    """How far the binary reaches, WITH the limits of the count attached.

    Every number here is a floor, and each reason it is a floor is named
    rather than left for the reader to discover: seats reaching the signer
    through signer.env are not counted, a seat whose HELM_CELL_BIN is a bare
    or relative name cannot be resolved in THIS process's PATH without
    borrowing our context, and a process whose environ could not be read is
    not evidence of absence.
    """
    if not resolved:
        return "how many seats reach it could not be measured"
    r = _cell.signer_reach(resolved)
    if r.get("err") or r.get("naming") is None:
        return "how many seats reach it could not be measured"
    parts = ["%d of %d live seats name it by absolute path"
             % (r["naming"], r["seats"])]
    if r.get("opaque"):
        parts.append("%d name it by a bare or relative path this process "
                     "cannot resolve in their context" % r["opaque"])
    if r.get("unreadable"):
        parts.append("%d processes were unreadable" % r["unreadable"])
    parts.append("seats reaching it through signer.env are not counted")
    return "; ".join(parts) + " — a floor"


def check_chat_node():
    """The chat room node (chat v2's signed transport): liveness, chain head,
    joined-cell balances (the never-die-on-computrons watch). Read-only —
    repairs live with `helm chat node up`."""
    from . import chat, chatnode as _node, pk as _pk
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
    # A USABLE SIGNER IS NOT A VERIFIED ONE, and until this line nothing on any
    # helm surface asked the difference. A dregg build whose archive exports no
    # verified ML-DSA cores does not fail — dregg-pq falls back to unaudited
    # implementations, the build stays green, and the single trace is one
    # stderr line at signer startup that no operator reads. A signer can
    # report BOTH cores absent while every other helm surface calls the
    # transport healthy, which is the state this row exists to name.
    #
    # UNKNOWN IS A WARN AND DEGRADED IS A FAIL, deliberately: not being able to
    # ask is not the same as a bad answer, and collapsing them would either cry
    # wolf on a signer helm cannot probe or stay silent on one it can.
    if signer["usable"]:
        cores = _cell.verified_cores()
        if cores["state"] == "degraded":
            degraded.append(_signer_unverified_row(cores))
        elif cores["state"] == "unknown":
            degraded.append((WARN, "chat signer verification UNKNOWN — %s"
                             % chat._safe_reason(cores["reason"])))
    if not url:
        return degraded + [(OK, "chat: signed transport disabled "
                                "(HELM_CHAT_NODE_URL empty) — v1 RAM room only")]
    head = chat.node_head(url)
    if head is None:
        # WHY IT IS NOT ANSWERING, WHEN THE UNIT CAN SAY: a node still in its
        # verified-runtime init is not down; a refusal with one known cure,
        # and a process running past the boot wait with no API, are FAILs
        # that say so (chatnode.unreachable_line, the same line `helm chat
        # node status` prints). A clean stop stays the plain WARN below.
        d = _node.boot_diagnosis(url)
        if d["state"] in ("initializing", "preparing"):
            return degraded + [(WARN, "chat room node " +
                                _node.unreachable_line(url, d))]
        if d["state"] in ("refused", "hung"):
            return degraded + [(FAIL, "chat room node " +
                                _node.unreachable_line(url, d))]
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
    # THE FAUCET IS A CELL AND IT CAN RUN DRY — its OWN state, above the cells
    # it could not fund. `POST /api/faucet` never mints (node/src/api.rs: value
    # enters only by genesis issuer-moves), so on a genesis-less cave the faucet
    # is a plain cell paying out of its own balance. When it empties, every row
    # below promising an auto-refund on the next post is promising a cure the
    # node cannot perform: measured with 20 cells at 0, a faucet holding 260 and
    # a grant that needed 697, this report printed twenty copies of that promise
    # and never once named the cell that caused all twenty.
    #
    # UNKNOWN IS NOT DRY AND NOT HEALTHY. A balance helm could not read says
    # nothing about the next grant, and the two ways to round it off are
    # opposite failures — one hides an outage, the other sends an operator to
    # refuel a faucet that is fine.
    faucet = _node.faucet_state(url)
    if faucet["state"] == "dry":
        out.append((FAIL, "chat FAUCET DRY — the room node pays every grant out "
                          "of cell %s, which holds %d, below the %d a funded "
                          "turn needs%s. POST /api/faucet NEVER MINTS, so no "
                          "auto-top-up can refund it: `%s`"
                    % (faucet["cell"][:12], faucet["balance"],
                       faucet["threshold"],
                       (" (the largest grant it refused asked %d)"
                        % faucet["observed_need"])
                       if faucet["observed_need"] else "",
                       "%s --amount %d" % (_node.REFUEL_VERB,
                                           _node.refill_amount(faucet)))))
    elif faucet["state"] == "unknown":
        out.append((WARN, "chat faucet balance UNKNOWN — %s; whether the faucet "
                          "can fund a turn is UNKNOWN, which is neither DRY nor "
                          "healthy" % faucet["reason"]))
    elif faucet["low"]:
        # LOW BEFORE DRY: the row that lands while the faucet can still pay.
        # DRY is named by the refusal that has already degraded every seat's
        # signed turn; this names the grants left and the refuel that restores
        # them, in the same sentence status and the wake print.
        out.append((WARN, "chat " + _node.low_line(faucet)))
    # AN UNREADABLE REGISTRY IS NOT AN EMPTY ONE. `read_json` answers {} for a
    # missing file AND for a malformed one, and `or {}` folds a null in with
    # them, so a corrupted .cells.json reported "0 funded of 0" — a clean bill
    # over a file nothing could read. The same absence-versus-unreadable split
    # this check already makes per CELL was missing at the FILE.
    cells_file = chat.cells_path()
    cells = _pk.read_json(cells_file, None)
    # UNREADABLE AND EMPTY MUST NOT SHARE AN OBSERVABLE. The old test refused
    # only None, so a file holding `[]` fell through `cells or {}` and reported
    # a confident "0 funded of 0" — a DEAD REGISTRY rendering as a healthy one,
    # which is the exact shape a doctor exists to catch. Worse, any TRUTHY
    # non-mapping ("oops", [1]) reached the iteration and crashed the whole
    # check, so one malformed file took out every other finding in the report.
    # The registry is a mapping of profile -> address and BOTH must be strings;
    # anything else is UNKNOWN, never fine.
    if os.path.exists(cells_file) and not (
            isinstance(cells, dict)
            and all(isinstance(k, str) and isinstance(v, str)
                    for k, v in cells.items())):
        out.append((WARN, "chat cells registry UNREADABLE at %s — whether any "
                          "cell is funded is UNKNOWN, not fine" % cells_file))
        return out
    cells = cells if isinstance(cells, dict) else {}
    # ONE ROW PER CLASS, NOT ONE PER CELL. Measured 2026-08-25: 16 of this
    # report's 30 WARN lines were the identical low-balance row, whose own text
    # says the auto-faucet fixes it on the next post. A self-healing condition
    # printed sixteen times is more than half the warnings on a surface agents
    # read to find the ones that are not self-healing, and the operator learns
    # to skim exactly the section that carries real findings. That is the same
    # cruft-blinds-the-instrument class as the 193 identical missing-home-dir
    # rows this file already folds four checks above.
    #
    # AND UNREADABLE IS NOT OK, WHICH IS THE HALF THAT WAS SILENT. A cell whose
    # node could not be reached returned `{}`, so `balance` was None, so the
    # `isinstance(bal, int)` test was False and the row printed at OK level
    # reading "balance ?". A failed look and a healthy cell shared a level;
    # the only tell was a question mark in prose nobody greps for.
    #
    # A ZERO BALANCE IS HEALTHY WHERE CHAT TURNS ARE FEE-FREE. helm declares
    # fee 0 for its coordination turns (cell.coord_fee, HELM_NODE_COORD_FEE)
    # and rides the node's exempt class, so a seat cell never needs a balance
    # and every seat cell on such a node sits at zero for good. Warning about
    # that was permanent noise; a cell there is either readable or it is not.
    fee_free = _cell.coord_fee() == 0
    low, blind, healthy = [], [], 0
    for profile in sorted(cells):
        info = _cell.get_json(url + "/api/cell/" + cells[profile], timeout=3) or {}
        bal = info.get("balance")
        if not isinstance(bal, int):
            blind.append(profile)
        elif bal < 200 and not fee_free:
            low.append(profile)
        else:
            healthy += 1
    # ONE ROW, BOUNDED, CARRYING THE WAY TO REACH THE REST. Sixteen identical
    # self-healing rows buried every other finding; naming every cell instead
    # let one append-only population bury them a different way. Both wrong
    # forms shipped here before this shape.
    if blind:
        out.append((WARN, "chat cell balance UNREADABLE for %d cell%s (%s) — "
                          "the node did not answer, so whether they are funded "
                          "is UNKNOWN, not fine"
                    % (len(blind), "s"[:len(blind) != 1],
                       _summary_names(blind, _CELL_DRILLDOWN, _FOLD_SHOW))))
    if low:
        # THE PROMISE IS CONDITIONAL ON THE FAUCET. Printed unconditionally it
        # is true while the faucet is funded and the exact opposite of the
        # truth once the faucet is the thing that ran out, which is when this
        # row's population is largest and its advice matters most. What tops a
        # cell up is the signer, before its next send (dregg-client-sign's
        # ensure_cell), out of the faucet — not a refund helm makes.
        refund = ("the signer tops it up before its next send; act only on a "
                  "name that is still here after it has posted")
        if faucet["state"] == "dry":
            refund = ("the signer's top-up CANNOT pay these: the faucet cell "
                      "itself is dry (see the FAUCET DRY row)")
        elif faucet["state"] == "unknown":
            refund = ("whether the signer's top-up can pay these is UNKNOWN: "
                      "the faucet cell's own balance could not be read")
        out.append((WARN, "chat cell balance low (<200) for %d cell%s (%s) — %s"
                    % (len(low), "s"[:len(low) != 1],
                       _summary_names(low, _CELL_DRILLDOWN, _FOLD_SHOW),
                       refund)))
    if healthy or not cells:
        out.append((OK, "chat cells: %d readable of %d — chat turns are "
                        "fee-free here (HELM_NODE_COORD_FEE=0), so a zero "
                        "balance is healthy" % (healthy, len(cells))
                    if fee_free else "chat cells: %d funded of %d"
                    % (healthy, len(cells))))
    return out


def check_chat_durability():
    """The chat write-behind leg: is the fleet's record actually on disk?

    Chat is RAM-first (tmpfs) with the journal as an append-only write-behind
    copy, so THE FLUSH TIMER IS THE DURABILITY. Measured 2026-08-11: the timer
    fired ~160 times over eight hours, wrote zero rows, and no surface anywhere
    said so — the owner found it by asking why memory was not on disk. The
    cursor age was the cheapest possible detector and nothing read it, so this
    row is that read.

    THE AGE COMES FIRST because it catches strictly more than the streak does:
    a streak can only count runs that HAPPEN, so a timer that stopped firing
    entirely — masked, failed unit, uninstalled — leaves the streak at zero
    forever while the age climbs.
    """
    from . import chat, home as _home
    h = chat.flush_health()
    if h["disabled"]:
        return [(WARN, "chat log-flush DISABLED (HELM_CHAT_LOG=%s) — the rooms "
                       "are tmpfs, so nothing written while it is off survives "
                       "a reboot" % (_home.env("CHAT_LOG") or "0"))]
    out = []
    age, interval = h["age_s"], h["interval_s"]
    if age is None:
        out.append((WARN, "chat log-flush has NEVER run on this host — every "
                          "room is RAM-only; `helm chat log-flush "
                          "--install-timer --apply`"))
    else:
        # THE THRESHOLDS LIVE WITH THE CADENCE THEY MULTIPLY (chat.py), so this
        # surface and the morning brief cannot drift into disagreeing about
        # when a journal is behind. At the installed 3-minute cadence that is
        # WARN past nine minutes, FAIL past an hour — the FAIL rung would have
        # named the measured outage at 07:37 instead of 10:27.
        level = (FAIL if age > chat.FLUSH_STALE_FAIL_INTERVALS * interval else
                 WARN if age > chat.FLUSH_STALE_WARN_INTERVALS * interval
                 else OK)
        # "LAST REACHED DISK", never "last success": the freshness can come
        # from the cursor advancing on a PARTIAL run, and a partial run is not
        # a success. Measured on my own probe — five consecutive failed runs
        # showed an age of 0s, correctly, because the healthy rooms among them
        # kept flushing. That is a true fact about durability and a false one
        # about health, so the streak row below carries health and this row
        # carries only what it actually knows.
        out.append((level, "chat durable flush: journal last reached disk %s "
                           "ago (timer every %ds)%s" % (
                               chat._dur(age), interval,
                               "" if level == OK else
                               " — every chat row since then is in RAM ONLY "
                               "and dies with the machine; run `helm chat "
                               "log-flush`")))
    if h["quarantined"]:
        out.append((WARN, "chat log-flush QUARANTINED %d room%s (%s) — %d row%s "
                          "stranded in RAM; a quarantined room stays undurable "
                          "until it is repaired" % (
                              len(h["quarantined"]),
                              "s"[:len(h["quarantined"]) != 1],
                              ", ".join(h["quarantined"][:3]),
                              h["stranded_rows"],
                              "s"[:h["stranded_rows"] != 1])))
    if h["streak"] >= chat.FLUSH_ALERT_STREAK:
        out.append((FAIL, "chat log-flush has failed %d consecutive runs — %s"
                          % (h["streak"], h["reason"] or "no reason recorded")))
    return out


def check_record():
    """The tool-outcome recorder: wired per claude home + state fresh
    (record.py owns the logic)."""
    from . import record
    return record.doctor_rows()


def check_resume_state():
    """The resume-turn state store (resumeturn.py owns the logic). Every
    compaction resume, deaf-in-effect nudge and episode wake DM reads it
    strictly and refuses as unknown when it cannot, so an unreadable store is
    a FAIL naming its path rather than a quiet loss of every resume."""
    from . import resumeturn
    return resumeturn.doctor_rows()


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


def check_skills_hub(dirs=None, canon=None):
    """Every claude config dir — each real credhome, the default ~/.claude,
    every seat and seat-instance CLAUDE_CONFIG_DIR — must carry `skills ->
    <hub>` (skillsync.canonical). One that does not loses every helm skill
    SILENTLY: no error at launch, /learn and /premise simply absent from each
    session there — the class in which a freshly prepared credhome ran seats
    for a day with no skills before anyone noticed. One FAIL per dir naming
    the path, what is there instead, and the one repair. No hub configured
    (the public default; distribution is opt-in) is no row. OK is said ONLY
    over a COMPLETE census: a subtree the census could not enumerate is a
    WARN naming it, never an OK over the dirs that happened to be visible.
    dirs/canon are test seams."""
    from . import registry, skillsync
    try:
        canon = canon or skillsync.canonical()
    except registry.AuthoredUnreadable as e:
        return [(WARN, "skills hub: authored layer unreadable (%s) — link "
                       "audit skipped" % e)]
    if not canon:
        return []
    canon = os.path.realpath(canon)
    if not os.path.isdir(canon):
        return [(FAIL, "skills hub: %s is configured but MISSING — every "
                       "config dir linked to it sees no skills" % canon)]
    if dirs is None:
        dirs = skillsync.config_dirs()
    # THE CENSUS'S OWN COMPLETENESS, read before any tally: a subtree the
    # census could not list is a config dir this check never saw, and a row
    # that says "N dir(s) link" over the ones it did see certifies exactly
    # the hidden one. A plain list from a test seam is a complete census.
    unlisted = list(getattr(dirs, "unlisted", ()))
    out, linked = [], 0
    for label, cdir in dirs:
        sdir = os.path.join(cdir, "skills")
        if os.path.islink(sdir):
            if os.path.realpath(sdir) == canon:
                linked += 1
                continue
            state = "-> %s, NOT the hub" % os.readlink(sdir)
        elif os.path.isdir(sdir):
            if os.path.realpath(sdir) == canon:
                continue                  # the hub itself, if ever listed
            state = "is a REAL dir, not a link to the hub"
        else:
            state = "MISSING"
        out.append((FAIL, "skills hub: %s %s %s — every session launched there "
                          "sees NO helm skills; `helm skills sync --apply` links "
                          "it to %s" % (label, sdir, state, canon)))
    if unlisted:
        out.append((WARN, "skills hub: census INCOMPLETE — could not list %s; "
                          "the %d dir(s) it could see link %s, but a config dir "
                          "hidden there is NOT audited; fix the listing, then "
                          "rerun" % (", ".join("%s (%s)" % u for u in unlisted),
                                     linked, canon)))
    elif not out:
        out.append((OK, "skills hub: %d config dir(s) link %s" % (linked, canon)))
    return out


def check_home_benefits(rows=None):
    """THE DRIFT REPORT over `homes.BENEFITS` — the one list of what every
    claude credential home carries, the same list `homes prepare` provisions
    from. One WARN per home that lacks something, naming each benefit, what
    is absent and the command that closes it; a separate WARN when a check
    could not tell, so an unreadable file never reads as present or missing.
    A benefit added to the list is audited here with no change to this rung.
    `rows` is a test seam (homes.benefit_drift's shape)."""
    from . import homes
    try:
        rows = homes.benefit_drift() if rows is None else rows
    except Exception as e:
        return [(WARN, "home benefits: drift report unavailable (%s: %s)"
                 % (e.__class__.__name__, e))]
    out = []
    for r in rows:
        if r["missing"]:
            out.append((WARN, "home benefits: %s (%s) lacks %s" % (
                r["label"], r["path"], "; ".join(
                    "%s — %s (%s)" % m for m in r["missing"]))))
        if r["unknown"]:
            out.append((WARN, "home benefits: cannot tell whether %s (%s) "
                              "carries %s" % (r["label"], r["path"], "; ".join(
                                  "%s — %s" % u for u in r["unknown"]))))
    if rows and not out:
        out.append((OK, "home benefits: %d claude home(s) carry every listed "
                        "benefit (%s)" % (len(rows), ", ".join(
                            b.name for b in homes.BENEFITS))))
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
    never-track staged-set scan — the 2026-07-29 leak proved a suite-run
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
                       "`%s` closes it (pre-commit runs the vacuity advisory "
                       "then never-track enforcement)%s"
                 % (root, ", ".join(stale), _guard.guard_remedy(root),
                    _guard.guard_remedy_note(root)))]
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


def check_authoring_disclosure():
    """Machine-authorship notes that have already reached published history.

    THE RUNG STOPS THE NEXT ONE; NOTHING STOPS THE LAST ONES. `trailer_rung`
    refuses an authoring line at commit-msg time and `launch` no longer names
    the seat to git, so the flow is closed — but history keeps what it was
    given, and rewriting shas already on trunk is worse than the disclosure.
    So this counts the standing debt instead of pretending the cure was
    retroactive.

    IT REPORTS TWO DIFFERENT DISCLOSURES BECAUSE THEY TRAVEL SEPARATELY. A
    trailer rides the MESSAGE; a seat name rides git's IDENTITY plane and is
    invisible to every message-time check. A run that found only one of them
    would read as a clean bill for the other.
    """
    out = []
    # THE ENVIRONMENT HALF, AND IT IS THE LIVE ONE: these variables OUTRANK
    # `git config`, so a seat carrying one publishes that name on every commit
    # it makes from here, whatever the repository is configured to say. Asked
    # first because it needs no repository and cannot fail.
    for key in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        value = os.environ.get(key)
        if value:
            out.append((WARN, "authoring disclosure: %s=%r is set in this "
                              "environment and OUTRANKS git config, so every "
                              "commit made here publishes that name"
                              % (key, value)))
    try:
        from . import vcs
        # THE TARGET IS PASSED, NEVER DERIVED FROM cwd, and the hazard is real
        # for this rung specifically: it answers about a REPOSITORY's published
        # history, so a backend that silently selected whichever repo the
        # process happened to be standing in would report another project's
        # commits under this one's name. `tests/test_vcs.py` refuses the
        # argumentless form by source scan for exactly that reason -- and by
        # plain regex over lines, so naming that form literally here, even in
        # a comment, trips it.
        here = os.getcwd()
        rc, text, _err = vcs.backend(here).text(
            here, "log", "--format=%H%x00%an%x00%B%x00%x00", "-n", "400")
    except Exception as exc:              # noqa: BLE001 — a rung that cannot
        out.append((WARN, "authoring disclosure: cannot read history (%s), so "
                          "published commits were NOT examined"
                          % type(exc).__name__))
        return out
    if rc != 0:
        out.append((WARN, "authoring disclosure: not a readable git repository "
                          "here, so published commits were NOT examined"))
        return out
    from . import trailer_rung
    seats = messages = scanned = 0
    for record in (text or "").split("\x00\x00"):
        parts = record.split("\x00")
        if len(parts) < 3:
            continue
        scanned += 1
        author, body = parts[1], parts[2]
        if trailer_rung.offending(body):
            messages += 1
        if author.strip().startswith(("helm-", "codex-", "claude-")):
            seats += 1
    if messages or seats:
        out.append((WARN, "authoring disclosure: of the last %d commits, %d "
                          "carry an AI authoring line and %d are authored by a "
                          "SEAT NAME. Already published — do NOT rewrite trunk; "
                          "the rung and the launch cure stop new ones"
                          % (scanned, messages, seats)))
    else:
        out.append((OK, "authoring disclosure: none in the last %d commits"
                        % scanned))
    return out


def check_harness_mirror():
    """Harness task lists that the ledger has not carried yet.

    THE LOOP IS THE FIX; THIS RUNG IS THE ALARM. A personal list is a free
    scratchpad, so work parked there is invisible by construction — the point
    of the mirror is that nobody has to notice, and the point of this rung is
    to say so out loud when the mirror has not run.

    The FINISHED tasks are reported as a NAMED POINTER rather than a silence.
    The scope ruling counts them instead of importing them, so that 446 finished
    scratchpad items do not bury a live board — and a count nobody prints is
    indistinguishable from work that was dropped.
    """
    out = []
    try:
        from . import tasksmirror
        rep = tasksmirror.sweep(apply=False)
    except Exception as exc:              # noqa: BLE001 — a rung that cannot
        return [(WARN, "harness mirror: cannot tell (%s)"   # look says so
                       % type(exc).__name__)]
    # A CADENCE NOBODY INSTALLED IS A LOOP NOBODY TURNS, and its absence is
    # silent by nature: the sweep simply never runs and the ledger simply
    # stays behind. So the rung reports the unit's absence rather than only
    # reporting its backlog — otherwise "182 unmirrored" reads as a busy fleet
    # instead of a cadence that was never switched on.
    try:
        from . import tasksmirror as _tm
        unit = _tm._timer_units()[2]
        if not os.path.exists(unit):
            out.append((WARN, "harness mirror: the cadence is NOT installed — "
                              "the loop only runs when someone types it. "
                              "`helm task mirror --ensure-timer`"))
    except Exception:                     # noqa: BLE001 — a rung that cannot
        pass                              # look must not take the sweep down
    if rep.get("dedup_unreadable"):
        # The sweep imported nothing and the backlog numbers below are all
        # zero, which without this line reads as a healthy, caught-up mirror.
        out.append((WARN, "harness mirror: the dedup source is UNREADABLE "
                          "(%s) — the sweep refuses rather than re-importing "
                          "every mirrored row, so the loop is STOPPED, not "
                          "idle" % rep["dedup_unreadable"]))
        return out
    live = len(rep["imported"])
    if live:
        out.append((WARN, "harness mirror: %d live task(s) in %d session(s) "
                          "not yet in the ledger — `helm task mirror --apply`"
                    % (live, rep["sessions"])))
    else:
        out.append((OK, "harness mirror: every live harness task is mirrored"))
    if rep["closed_unmirrored"]:
        out.append((OK, "harness mirror: %d finished scratchpad task(s) "
                        "unmirrored by scope ruling — counted, not lost "
                        "(`helm task mirror --all-statuses`)"
                    % rep["closed_unmirrored"]))
    # ONE WARNING FOR THE WHOLE CLASS, never one per instance. A rung that
    # emits a line per unreadable file buries the finding it exists to raise —
    # the flood IS the burial, which is why `_folded` exists in this module.
    if rep["unreadable"]:
        bad = rep["unreadable"]
        shown = ", ".join(os.path.basename(b) for b in bad[:3])
        if len(bad) > 3:
            shown += ", +%d more" % (len(bad) - 3)
        out.append((WARN, "harness mirror: %d harness task file(s) will not "
                          "parse (%s) — REPORTED rather than skipped, because "
                          "a loop that stops work vanishing must not vanish "
                          "what it cannot read" % (len(bad), shown)))
    return out


def check_keepalive_cadence():
    """The credential keepalive's cadence and its last recorded grant.

    THE LOOP NOBODY INSTALLED IS THE ONE THAT BURNS AN ACCOUNT'S COPY. helm's
    one sanctioned credential writer rolls an idle home's token forward before
    its refresh chain rots; while it was on no cadence it ran only when
    somebody typed it, and the quota page told the owner that healthy accounts
    needed re-logins. So the absence of the unit is a finding here, and so is a
    last grant older than the token lifetime the loop exists to stay ahead of.

    Reads only the unit path and keepalive's own log. Running the sweep from
    doctor would put a credential WRITE on the doctor path, which no rung may
    do — the stale-bot rung refuses its own sweep for the milder version of
    the same reason."""
    out = []
    try:
        from . import keepalive
        installed = keepalive.timer_installed()
        last = keepalive.last_refresh()
        interval = keepalive.DEFAULT_INTERVAL_S
        hand = keepalive.hand_crontab()
    except Exception as exc:              # noqa: BLE001 — a rung that cannot
        return [(WARN, "keepalive cadence: cannot tell (%s)"   # look says so
                       % type(exc).__name__)]
    if not installed:
        out.append((WARN, "keepalive cadence: the timer is NOT installed — the "
                          "one verb that refreshes helm's copy of an account's "
                          "token only runs when someone types it, and an "
                          "unrefreshed copy reads as an account needing a "
                          "re-login. `helm keepalive --ensure-timer`"))
    # A HAND-INSTALLED LINE IS REPORTED, NEVER REMOVED: the crontab is the
    # operator's file and nothing here can prove which of its lines are ours.
    for line in hand:
        out.append((WARN, "keepalive cadence: a crontab line runs this verb by "
                          "hand%s — `crontab -e` removes it: %s"
                    % (" and is superseded by the installed timer" if installed
                       else " (install the timer with `helm keepalive "
                            "--ensure-timer`, then remove it)", line)))
    if not last:
        out.append((WARN, "keepalive cadence: no refresh is recorded in "
                          "keepalive's log — either it has never granted on "
                          "this box or the log was cleared. `helm keepalive` "
                          "(dry-run) shows what is due"))
        return out
    age = _keepalive_age_s(last.get("ts"))
    if age is None:
        out.append((OK, "keepalive cadence: last grant %s by %s (timestamp not "
                        "parseable, so its age is unknown)"
                    % (last.get("ts"), last.get("by"))))
        return out
    line = ("keepalive cadence: last grant %dh ago by %s%s"
            % (age // 3600, last.get("by"),
               "" if installed else " — and no timer installed"))
    # THE BAR IS THE TOKEN, NOT THE TIMER. An anthropic access token lives
    # about 8h; a loop that last granted longer ago than that has already let
    # at least one home go dead no matter what its configured period says.
    if age > max(TOKEN_LIFETIME_S, 2 * interval):
        out.append((WARN, line + " — PAST THE TOKEN LIFETIME (%dh) this loop "
                          "exists to stay ahead of: the timer looks dead "
                          "(`systemctl --user status %s`), and homes it has "
                          "not rolled forward read as accounts wanting a "
                          "re-login" % (TOKEN_LIFETIME_S // 3600,
                                        keepalive.TIMER_NAME)))
    else:
        out.append((OK, line))
    return out


def _keepalive_age_s(ts, now=None):
    """Seconds since a keepalive log timestamp (%Y-%m-%dT%H:%M:%S%z), or None
    when it will not parse. A rung never guesses an age it cannot read."""
    import datetime
    try:
        when = datetime.datetime.strptime(str(ts), "%Y-%m-%dT%H:%M:%S%z")
    except (TypeError, ValueError):
        return None
    ref = now if now is not None else time.time()
    return max(0, int(ref - when.timestamp()))


# The anthropic access-token lifetime the grant hands back (expires_in 28800).
# Named here because this rung's bar is the TOKEN, never the configured period.
TOKEN_LIFETIME_S = 8 * 3600

#: HOW MANY PROBES MAKE A STREAK MORE THAN A BAD NIGHT. Two passes that both
#: saw nothing cannot be one transient refusal, and one is exactly what a
#: network blip writes. The bar on the DURATION is the token's own lifetime
#: (above), so both conjuncts are imported rather than minted.
STREAK_MIN_PROBES = 2


def check_cred_copy_staleness():
    """Per-credential: how long has helm's OWN copy been answering no reading?

    THE SEAM THIS RUNG FILLS, and it went unwatched for weeks. Two loops touch
    these credentials and neither asks this question. `keepalive` rolls the
    homes whose refresh chain it can still grant on — a home whose chain is
    SPENT is skipped by it forever. `check_keepalive_cadence` then reports THE
    WRITER: timer installed, last grant recent. Both read green while one
    account's copy was dead, because every OTHER home kept rolling forward.
    The probe meanwhile kept writing `reauth-needed` for that account on every
    pass, thousands of times, and the only consumer that ever acted on it was
    a colour — which rationed the fleet rather than naming the repair.

    THE TWO CONJUNCTS ARE THE WHOLE RUNG. A duration alone accuses an account
    that nothing has probed lately, which is a fact about the probe; a probe
    count alone fires on one bad night. Together they say the only thing worth
    an operator's attention: the loop RAN, it kept looking, and it kept seeing
    nothing — so this is not a gap in the reading, it is a dead copy.

    IT ACCUSES THE COPY, NEVER THE ACCOUNT. `providers` already wrote which of
    the two it is into the status, and that sentence is carried WHOLE (the
    difference between `helm cred sync-orca` and an owner re-login lives in
    its parenthesis, exactly as `burst` documents). Where the streak is on the
    credential THIS SEAT IS HOMED ON, the rung says so — a seat serving turns
    on a credential its own reader calls unreadable is the proof that the
    account is fine and our copy is not.

    WARN-ONLY, and reads FILES: the observation log and this seat's home
    identity. A rung that refreshed a token would put a credential WRITE on
    the doctor path, which no rung may do."""
    out = []
    try:
        from . import burnflags
        rows = burnflags.unread_streaks()
    except Exception as exc:              # noqa: BLE001 — a rung that cannot
        return [(WARN, "cred copy staleness: cannot tell (%s)"  # look says so
                       % type(exc).__name__)]
    homed = None
    try:
        from . import burst
        homed = burst.homed_account()[1]
    except Exception:                     # noqa: BLE001 — the conjunct is a
        homed = None                      # bonus, never the finding
    loud = [r for r in rows
            if r["unread_s"] > TOKEN_LIFETIME_S
            and r["probes_since"] >= STREAK_MIN_PROBES]
    if not loud:
        out.append((OK, "cred copy staleness: no credential's copy has been "
                        "unreadable for longer than the %dh token lifetime "
                        "across %d or more probes (%d account(s) in the log)"
                    % (TOKEN_LIFETIME_S // 3600, STREAK_MIN_PROBES,
                       len(rows))))
        return out
    for r in sorted(loud, key=lambda r: -r["unread_s"]):
        days = r["unread_s"] // 86400
        span = ("at least %dd (no readable reading anywhere in the retained "
                "log, so this is a FLOOR)" % days if r["floor_only"]
                else "%dd" % days)
        mine = (" — AND THIS SEAT IS HOMED ON IT, so it is serving turns right "
                "now on a credential helm's own reader calls unreadable"
                if homed and r["credential"] == homed else "")
        out.append((WARN, "cred copy staleness: helm's token copy for %s has "
                          "answered NO READING for %s, across %d probe(s) "
                          "since its last readable one, while the probe kept "
                          "running (newest pass %dm ago)%s. The probe's own "
                          "repair: %s"
                    % (r["credential"], span, r["probes_since"],
                       r["newest_probe_age_s"] // 60, mine, r["status"])))
    return out


def check_stale_bot():
    """The stale-bot's own liveness + its last sweep's report.

    A WATCHER WHOSE DEATH IS SILENT IS THE DEFECT IT HUNTS: the sweep exists
    because aged rows sat unwatched, so a dead timer must read as a finding
    here, never as quiet. This rung deliberately reads only the STATE FILE the
    sweep records — running the sweep from doctor would put every aged row's
    git probes on the doctor path (the harness-mirror rung can afford its dry
    pass because it is filesystem reads; this one is not)."""
    out = []
    try:
        from . import stalebot
        st = stalebot.read_state()
        unit = stalebot._timer_units()[2]
        interval = stalebot.DEFAULT_INTERVAL_S
    except Exception as exc:              # noqa: BLE001 — a rung that cannot
        return [(WARN, "stale-bot: cannot tell (%s)"       # look says so
                       % type(exc).__name__)]
    if not os.path.exists(unit):
        out.append((WARN, "stale-bot: the cadence is NOT installed — the "
                          "housekeeping loop only runs when someone types it. "
                          "`helm stale sweep --ensure-timer`"))
    last = float(st.get("last_run") or 0)
    if not last:
        out.append((WARN, "stale-bot: has never recorded a sweep — aged rows "
                          "are unwatched. `helm stale sweep`"))
        return out
    import time as _time
    age = _time.time() - last
    line = ("stale-bot: last sweep %dh ago — %d row(s) swept, %d proposal(s) "
            "filed in %d digest(s), oldest unproposed %dh"
            % (age // 3600, int(st.get("swept") or 0),
               int(st.get("proposed") or 0),
               int(st.get("digests_posted") or 0),
               int(st.get("oldest_unproposed_s") or 0) // 3600))
    if age > 2 * interval:
        out.append((WARN, line + " — PAST TWICE ITS CADENCE: the timer looks "
                          "dead (`systemctl --user status "
                          "helm-stale-bot.timer`)"))
    else:
        out.append((OK, line))
    if st.get("digests_failed"):
        out.append((WARN, "stale-bot: %d digest(s) FAILED to post last sweep "
                          "— their rows stay unlatched and re-propose"
                    % int(st.get("digests_failed") or 0)))
    # THE BACKLOG BEHIND THE CAP IS A FINDING, NOT A FOOTNOTE. Rows past a
    # digest's 12-line cap are deliberately left unlatched so the next sweep
    # carries them, which is correct and also means an owner can sit behind a
    # standing overflow indefinitely while every digest still looks healthy.
    # This count is the only surface that shows it.
    if st.get("capped_unlatched"):
        out.append((WARN, "stale-bot: %d row(s) did not fit last sweep's "
                          "digests — unlatched by design and carried to the "
                          "next sweep, but a standing overflow means an owner "
                          "is behind the cap every run (`helm stale sweep "
                          "--json` lists them)"
                    % int(st.get("capped_unlatched") or 0)))
    if st.get("rearm_suspended"):
        out.append((WARN, "stale-bot: re-arm was SUSPENDED last sweep because "
                          "a source was unreadable (%s) — latches were kept "
                          "rather than dropped, so a row that HAS left the "
                          "population may stay silent one extra window"
                    % ("; ".join(str(u) for u in
                                 (st.get("unavailable") or ()))[:160]
                       or "reason not recorded")))
    return out


def check_intent_actual():
    """DECLARED intent vs OBSERVED wiring — the half-configured-is-invisible
    class, starting with the codex credential pool.

    THE CLASS: nothing else in helm compares what the operator INTENDED to
    what is ACTUALLY wired, so a fleet running at less than its paid capacity
    looks exactly like a healthy one until it fails.

    WHERE INTENT LIVES: <seat_dir>/<family>/intent.json — OPERATOR-authored,
    never helm-minted. That is the whole deliberately-fewer-vs-accidentally-
    fewer distinction, and it has to be operator-authored or the distinction
    is fake: a rung that wrote its own baseline from whatever it first
    observed would bless the half-configured state it exists to catch, and a
    hardcoded expectation in helm would be the deployment fact that adapts
    worst of all (hardcode-is-eventual-failure). The file is the operator
    saying "this is what I mean to have"; the rung only ever reads it.

    THREE STATES, NEVER TWO. UNDECLARED (no intent file) is reported as ONE
    quiet OK line — visible that the rung is alive and unarmed, never a WARN:
    a rung that screams about every undeclared family teaches its reader to
    dismiss it, which is the must-miss this whole class dies of. DECLARED and
    MATCHED is OK with the observed counts. DECLARED and UNDER is WARN — and
    it names the OBSERVED SIDE (accounts, tier counts, dead files), never a
    bare count, because a classifier that reduces evidence to a label owes
    the reader the evidence that would let them disagree.

    THE OBSERVED SIDE IS THE POOL'S OWN MACHINERY (codex_pooled), read by
    ACCOUNT (account_id), never by FILE — one account pooled into two files
    is one credential, and counting files would double it. The tier
    vocabulary is codexhomes' own (tier()), never re-derived here, so the
    rung's answer means the same thing as the proxy's. A file that will not
    parse is counted as wired-but-dead harm and named, because it occupies
    the pool slot it would otherwise fill.

    IDENTITY IS THE ACCOUNT ID, FIRST AND ALWAYS, and the four rules under it
    are one shape rather than four patches:

      * an EMAIL resolves an account and never merges two. It is an alias
        edge only when it is a REAL address, because the pool writer spells
        the email of every credential with no email claim with one fixed
        placeholder, and a union over that string would collapse every such
        account into one.
      * the answer is a FUNCTION OF THE WHOLE POOL, never of the order the
        dir sorted in: the address map is built from every record before any
        record is keyed, so all permutations of one pool agree.
      * a row whose identity cannot be READ (a non-string field, an absent
        one) or cannot be resolved UNAMBIGUOUSLY is COUNTED as its own unit —
        never skipped, which under-reports wiring, and never merged, which
        invents capacity — and it is named, because a total that may be high
        owes its reader the rows that made it so.
      * a TIER is a fact about an account, so two records of one account that
        disagree are a CONFLICT credited to no tier, never first-file-wins.

    ONE ACCOUNT IS ONE UNIT ONLY ON A PERSONAL PLAN (task/2981). A Team
    account id is a WORKSPACE that every member carries, so an account whose
    plans are not all KNOWN to be personal counts by MEMBER (account id plus
    user id, as `codexresets.member_id` keys a credential), and a member it
    cannot prove is counted on its own and named, never folded.

    AND EVERY INPUT THE RUNG CANNOT READ YIELDS UNKNOWN, NEVER ZERO AND NEVER
    A CRASH: an unreadable intent file, a missing pool dir, a pool dir that
    exists but cannot be ENUMERATED, a pool dir that PASSED the stat here and
    was gone by the time the reader listed it, and a pool whose every file
    failed to parse. The pool reader never raises and never answers `[]` to a
    failure: it answers a census whose KIND says which of those happened, and
    this rung reads that kind — a reader that reduced all of them to an empty
    list would hand this rung the same value an empty pool does. Negative and unreadable must not share a value — a
    measured shortfall is a claim about the fleet, and the rung may only make
    it about a pool it actually read."""
    from . import codexhomes
    path = os.path.normpath(
        os.path.join(codexhomes.pool_dir(), os.pardir, "intent.json"))
    try:
        with pk.open_regular(path) as fh:
            import json as _json
            intent = _json.load(fh)
    except FileNotFoundError:
        return [(OK, "intent-vs-actual: no codex intent declared "
                       "(no %s) — the rung is unarmed; declare the paid "
                       "account tiers there to arm it" % path)]
    except (OSError, ValueError) as e:
        return [(WARN, "intent-vs-actual: codex intent at %s is "
                       "UNREADABLE (%s: %s) — cannot compare intent to "
                       "wiring" % (path, e.__class__.__name__, e))]
    accounts = intent.get("accounts") if isinstance(intent, dict) else None
    if not isinstance(accounts, dict) or not accounts \
            or not all(type(v) is int and v >= 0
                       for v in accounts.values()):
        return [(WARN, "intent-vs-actual: codex intent at %s does not "
                       "name account tier counts ({\"accounts\": "
                       "{\"ultra\": N, \"team\": M}}) — refusing to guess "
                       "the intent it cannot read" % path)]
    auth_dir = codexhomes.pool_dir()
    if not os.path.isdir(auth_dir):
        return [(WARN, "intent-vs-actual: the codex credential pool %s does "
                       "not exist or is unreadable — whether anything is "
                       "wired is UNKNOWN, never a measured shortfall"
                 % auth_dir)]
    # AN EMPTY ANSWER AND AN UNREADABLE POOL MUST NOT SHARE A VALUE (task/2480
    # F1/F2). The pool reader enumerates the dir ONCE by name and reports WHICH
    # of four things happened, because a reader that answered `[]` to a failure
    # would hand the arithmetic below a silence it renders as "0 wired of N
    # declared" — a measured shortfall against a pool that was never read. The
    # isdir guard above cannot catch either case this branch handles:
    #
    #   UNREAD — the dir is THERE and will not enumerate (EACCES, EIO, a
    #   regular file where a directory belongs). isdir sees a directory.
    #
    #   MISSING — the dir was there at the isdir above and is GONE by the time
    #   the reader lists it: an `unpool`, a rename, a tmpfs remount between two
    #   syscalls. For the budget, "no pool dir" is a real empty pool and that
    #   reading is allowed to retire a refusal; for THIS rung it is not, and
    #   round 3 lost the discriminator that said so. The reader maps a missing
    #   directory to NO RECORDS, which is byte-identical to an empty pool, so
    #   the whole distinction has to be made on the KIND — and nowhere else.
    #
    # An empty pool that ENUMERATED falls through below and is a real
    # shortfall, which is the answer that must survive both of these.
    census = codexhomes.read_pool()
    if census.kind == codexhomes.POOL_UNREAD:
        return [(WARN, "intent-vs-actual: the codex credential pool %s could "
                       "not be READ (%s) — what is wired is UNKNOWN, "
                       "never a measured zero, because a check that cannot "
                       "run must not read as satisfied"
                 % (auth_dir, census.error))]
    if census.kind == codexhomes.POOL_MISSING:
        return [(WARN, "intent-vs-actual: the codex credential pool %s was "
                       "there a moment ago and is GONE now — what is wired is "
                       "UNKNOWN, never a measured zero: this rung read no "
                       "pool at all, so it may not report a shortfall against "
                       "one" % auth_dir)]
    # No try here on purpose: `pooled_rows` is pure over records the census
    # already parsed, so there is nothing left for it to fail at. A guard
    # around a call that cannot raise is a guard nothing tests.
    pooled = codexhomes.pooled_rows(census)
    live, dead, off, units = {}, [], [], []

    def _spelling(value):
        """The stripped, casefolded spelling of an identity field, or None when
        the record carries no READABLE one.

        A POOL FILE IS JSON ON DISK AND ITS FIELDS ARE WHATEVER IS IN IT.
        `codex_pooled` passes `email`/`account_id` straight through, so a
        hand-edited or half-written record can carry a number, a dict or a list
        where a spelling belongs — and a bare `.casefold()` on it takes the
        WHOLE `helm doctor` run down with an AttributeError, so every rung
        after this one stops reporting because of one bad pool file. An account
        is not case-sensitive, hence the fold."""
        return (value.strip().casefold()
                if isinstance(value, str) and value.strip() else None)

    def _address(value):
        """The spelling of an email that may carry an ALIAS EDGE, else None.

        AN ALIAS EDGE NEEDS A REAL ADDRESS. The pool writer
        (`translate_codex_auth`) spells the email of any credential whose
        id_token carries no email claim with a fixed PLACEHOLDER, so every such
        account in the pool shares one email string — and an alias over that
        string merges accounts that have nothing to do with each other. The
        test is STRUCTURAL rather than a blacklist of the placeholder of the
        day, which would be pinned to nothing and would miss the next one."""
        spelling = _spelling(value)
        return spelling if spelling and "@" in spelling else None

    def _named(rec):
        """What to CALL a record in operator-facing prose: its own spelling,
        never folded, and never a non-string — which would take the `join` in
        every note below down the same way a bare casefold takes the census
        down."""
        for value in (rec.get("email"), rec.get("account_id"), rec.get("file")):
            if isinstance(value, str) and value.strip():
                return value
        return "?"

    for row, rec in enumerate(pooled):
        if rec.get("error"):
            dead.append(rec.get("file") or "?")
            continue
        # LIVENESS FILTERS THE COUNT, NEVER THE IDENTITY GRAPH. A disabled or
        # expired bridge can be the only record joining a live legacy spelling
        # to its live refreshed account id. Discarding it here splits one live
        # account into two. Every parseable record therefore contributes its
        # identity edges; only `live` records vote on tier and enter the count.
        enabled = not rec.get("disabled") and rec.get("type") == "codex"
        if not enabled:
            # A wired account that is OFF is not absent and not live — naming
            # it is what keeps the remedy honest: the cure is REFRESH OR
            # RE-POOL, never a fresh login, which is how a pooled account
            # grows a duplicate credential.
            off.append(_named(rec))
        # EXPIRY IS A TIME, NOT A FLAG, AND A PRESENT-BUT-UNPARSEABLE ONE IS
        # OFF. The pooled record's `expired` carries the access token's RFC3339
        # expiry. A HEALTHY credential's is in the future and truthy, so a
        # boolean check marks it off (the first cure's P0). A `Z` suffix breaks
        # fromisoformat on Python 3.9/3.10 (the second). AND a value that IS
        # THERE but cannot be read — an unparseable STRING, or a truthy
        # NON-STRING the layer never mints (True, a number) — is the proxy
        # recording something it could not vouch for, and counts OFF. Only an
        # ABSENT value (None, what the layer emits when it has no claim) is not
        # evidence of expiry and stays live.
        expired = False
        exp = rec.get("expired")
        if enabled and exp is not None:
            if not isinstance(exp, str):
                expired = True            # a non-string is not a timestamp
            elif exp:
                import datetime as _dt
                try:
                    when = _dt.datetime.fromisoformat(
                        exp[:-1] + "+00:00" if exp.endswith(("Z", "z")) else exp)
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=_dt.timezone.utc)
                    expired = when <= _dt.datetime.now(_dt.timezone.utc)
                except ValueError:
                    expired = True        # present but unreadable: cannot vouch
        if expired:
            off.append("%s (expired)" % _named(rec))
        fname = rec.get("file")
        user = rec.get("user_id")
        units.append({"account": _spelling(rec.get("account_id")),
                      "address": _address(rec.get("email")),
                      "user": user if isinstance(user, str) and user else None,
                      "plan": rec.get("plan"),
                      "tier": rec.get("tier"), "row": row,
                      "file": fname if isinstance(fname, str) and fname
                              else "?", "live": enabled and not expired})
    # IDENTITY IS THE ACCOUNT ID, FIRST AND ALWAYS; AN EMAIL RESOLVES ONE AND
    # NEVER MERGES TWO. One credential is spelled several ways across the pool
    # — a legacy record carrying only the old email, a record carrying
    # {account_id, old email}, a refreshed record carrying {account_id, new
    # email} — so an email has to be able to FIND the account it belongs to or
    # the legacy copy counts twice. But an email is not itself an identity: two
    # records naming DIFFERENT account_ids are two accounts however their
    # emails are spelled, and a union that puts both namespaces in one bag
    # collapses them the moment they share a spelling (which the writer's
    # placeholder email guarantees for every credential with no email claim).
    #
    # AND THE ANSWER IS A FUNCTION OF THE WHOLE SET, NEVER OF THE READ ORDER.
    # The incremental form — decide each record against the records seen SO FAR
    # — is order-dependent by construction: with the bridging record read LAST,
    # both endpoints are already counted and the bridge only ever marks ITSELF
    # a duplicate. So the address map is built from EVERY record before ANY
    # record is keyed, and the components are then the same under every
    # permutation of the pool.
    owners = {}
    for unit in units:
        if unit["address"] and unit["account"]:
            owners.setdefault(unit["address"], set()).add(unit["account"])
    for unit in units:
        if unit["account"]:
            unit["key"] = ("account", unit["account"])
        elif unit["address"] and len(owners.get(unit["address"], ())) == 1:
            unit["key"] = ("account", next(iter(owners[unit["address"]])))
        elif unit["address"] and not owners.get(unit["address"]):
            unit["key"] = ("address", unit["address"])
        else:
            # NO IDENTITY, OR AN AMBIGUOUS ONE (an address that two DIFFERENT
            # account_ids both spell), IS COUNTED AS ITS OWN UNIT — never
            # skipped, never merged. Cannot-dedupe-then-COUNT-it is the honest
            # direction: skipping the row reports capacity BELOW what is wired
            # and prescribes a login for an account that is already there,
            # while folding it into a neighbour reports a credential the proxy
            # may not have. It is NAMED as harm below, because a count that may
            # be high owes its reader the rows that made it so.
            unit["key"] = ("row", unit["row"])
    # AN ACCOUNT ID NAMES A CREDENTIAL ONLY ON A PERSONAL PLAN (task/2981). On
    # a Team plan it is the WORKSPACE, and every member carries it, so three
    # members wired as three credentials counted as ONE and the rung reported
    # a shortfall against capacity that was live. So the account falls back
    # to ONE unit only when every plan its records carry is KNOWN to be
    # personal (`codexhomes.personal_plan`, the proxy's own vocabulary). Any
    # other account (team, a plan helm has no word for, no plan at all, or a
    # disagreement) splits by MEMBER: the account id plus the user id, as
    # `codexresets.member_id` keys a credential. A record with no user-id
    # claim is its member by its address, joined to the one user id that
    # address carries on the account. One with neither is counted on its own
    # and named: never folded into a sibling it cannot be proven to be.
    plans = {}
    for unit in units:
        if unit["key"][0] == "account":
            plans.setdefault(unit["key"], set()).add(unit["plan"])
    users = {}
    for unit in units:
        if unit["user"] and unit["address"]:
            users.setdefault((unit["key"], unit["address"]), set()).add(
                unit["user"])
    for unit in units:
        key = unit["key"]
        known = {p for p in plans.get(key, ()) if p}
        if key[0] != "account" or (known and all(
                codexhomes.personal_plan(p) for p in known)):
            continue
        by_address = users.get((key, unit["address"]), ())
        if unit["user"]:
            unit["key"] = ("member", key[1], "user " + unit["user"])
        elif len(by_address) == 1:
            unit["key"] = ("member", key[1], "user " + next(iter(by_address)))
        elif unit["address"] and not by_address:
            unit["key"] = ("member", key[1], "address " + unit["address"])
        else:
            unit["key"] = ("row", unit["row"])
    # A TIER IS A FACT ABOUT AN ACCOUNT, AND TWO RECORDS OF ONE ACCOUNT THAT
    # DISAGREE HAVE NO ANSWER. The pool is read in filename order, so crediting
    # the first record's tier is FIRST-FILENAME-WINS: a stale copy pooled under
    # the old plan silently beats the refreshed one, and the census prints a
    # tier breakdown nothing in the pool actually says. A DISAGREEMENT IS A
    # CONFLICT the operator has to see, and the account is credited to NEITHER
    # tier until it is resolved — guessing one is the bug, not the cure. An
    # ABSENT tier is not a disagreement (missing evidence is not evidence
    # against), so only the tiers records actually CARRY vote.
    votes, conflicts = {}, []
    for unit in units:
        if not unit["live"]:
            continue
        by_tier = votes.setdefault(unit["key"], {})
        if isinstance(unit["tier"], str) and unit["tier"]:
            by_tier.setdefault(unit["tier"], []).append(unit["file"])
    for key, by_tier in sorted(votes.items()):
        if len(by_tier) > 1:
            conflicts.append("%s %s pooled as %s" % (key[0], key[1], " and ".join(
                "%s (%s)" % (t, ", ".join(sorted(files)))
                for t, files in sorted(by_tier.items()))))
            continue
        live.setdefault(next(iter(by_tier), "?"), set()).add(key)
    # KEYED ON THE ROW, NOT ON THE FILENAME: two rows the rung cannot
    # identify must not become one unit because their filenames happened to be
    # unreadable the same way. The filename is for the operator to read; the
    # row is what keeps them apart.
    unresolved = sorted(u["file"] for u in units
                        if u["live"] and u["key"][0] == "row")
    shortfalls, surpluses = [], []
    for t, want in sorted(accounts.items()):
        got = len(live.get(t, ()))
        if got < want:
            shortfalls.append(
                "%s: %d wired of %d declared" % (t, got, want))
        elif got > want:
            # DECLARED-ZERO IS A DECLARATION TOO: a tier the operator says
            # should hold nothing is a surplus the moment anything wires, and
            # the want>0 guard would hide it exactly there.
            surpluses.append(
                "%s: %d wired against %d declared" % (t, got, want))
    extra = sorted(set(live) - set(accounts))
    for t in extra:
        if live[t]:
            surpluses.append(
                "%s: %d wired with NO declared tier" % (t, len(live[t])))
    observed = ", ".join("%s=%d" % (t, len(s))
                         for t, s in sorted(live.items())) or "none"
    remedy = ("the missing accounts need the operator's own login — or, if "
              "they are already authenticated in a managed codex home, "
              "pooling (`helm codex sync-orca`)")
    dead_note = ("; unparseable pool files: %s" % ", ".join(sorted(dead))
                 if dead else "")
    off_note = ("; wired but off (refresh or re-pool, do not re-login): %s"
                % ", ".join(sorted(off)) if off else "")
    # A COUNT THAT MAY BE HIGH OWES ITS READER THE ROWS THAT MADE IT SO. A row
    # whose identity cannot be read (or is ambiguous) is counted on its own
    # rather than skipped, which is exactly where the observed total can exceed
    # the credentials that exist — and a number with no evidence beside it is
    # the classifier this rung refuses to be.
    id_note = ("; identity unreadable or ambiguous, counted on its own so the "
               "observed total may be HIGH: %s" % ", ".join(unresolved)
               if unresolved else "")
    conflict_note = ("; tier CONFLICT (credited to NO tier until resolved — "
                     "re-pool the account under one plan): %s"
                     % "; ".join(conflicts) if conflicts else "")
    # A pool that yields NOTHING USABLE has THREE honest shapes and they must
    # not share one verdict. EVERY FILE UNPARSEABLE means the rung could not
    # look — that is UNKNOWN, never a measured zero, because an unreadable pool
    # collapsing to a count is the failure this rung exists to refuse. EVERY
    # ACCOUNT OFF is a MEASURED fact: the files parsed, so the rung knows
    # exactly what is wired (everything, disabled), and calling that UNKNOWN
    # hides the one thing the operator most needs named. AND THE MIXED POOL IS
    # NEITHER: some files parsed and some did not, so the all-unparseable
    # sentence is a lie about the account the rung read perfectly well — whose
    # remedy is the actionable half — while the every-account-off sentence is a
    # claim about a population the rung never finished enumerating. Ordering
    # the first two branches on `dead` alone gave the mixed pool to the
    # all-unparseable one, which is why it needs the parsed count, not a
    # priority.
    parsed = len(pooled) - len(dead)
    if not any(live.values()) and dead and not parsed:
        return [(WARN, "intent-vs-actual: the codex pool yielded NO usable "
                       "credential (all files unparseable) — whether anything "
                       "is wired is UNKNOWN, never a measured zero%s"
                 % dead_note)]
    if not any(live.values()) and off and dead:
        return [(WARN, "intent-vs-actual: NO codex account is live — every "
                       "account that PARSED is off (%s), and the file(s) that "
                       "did not parse could hold more, so the total is "
                       "UNKNOWN; refresh or re-pool, do not re-login%s%s%s"
                 % (", ".join(sorted(off)), dead_note, id_note,
                    conflict_note))]
    if not any(live.values()) and off:
        return [(WARN, "intent-vs-actual: EVERY codex account is OFF (%s) — "
                       "the pool is wired but none of it is live. This is a "
                       "measured state, not an unknown one; refresh or "
                       "re-pool, do not re-login%s%s%s"
                 % (", ".join(sorted(off)), dead_note, id_note,
                    conflict_note))]
    if shortfalls:
        detail = "; ".join(shortfalls)
        if surpluses:
            detail += "; surplus: " + "; ".join(surpluses)
        return [(WARN, "intent-vs-actual: codex credentials UNDER intent — "
                       "%s (observed live: %s). Part of what is paid for is "
                       "not wired and nothing else says so; %s%s%s%s%s"
                 % (detail, observed, remedy, off_note, dead_note, id_note,
                    conflict_note))]
    if surpluses:
        # DEAD FILES ARE HARM EVEN HERE: a surplus line that omits them lets
        # wired-but-dead slots hide behind the excess — and so do off
        # accounts, which a surplus would otherwise swallow whole.
        return [(WARN, "intent-vs-actual: codex credentials EXCEED intent — "
                       "%s (observed live: %s). More is wired than declared; "
                       "either the intent file is stale or an account is "
                       "pooled that should not be%s%s%s%s"
                 % ("; ".join(surpluses), observed, off_note, dead_note,
                    id_note, conflict_note))]
    if dead or off or unresolved or conflicts:
        # EXACT MATCH IS NOT ALL-CLEAR WHEN ANY PART OF THE POOL IS OFF, DEAD,
        # UNIDENTIFIABLE OR SELF-CONTRADICTING. Every harm is named together,
        # because separate branches would let the one that fires first swallow
        # the others — an off account hidden behind an unparseable file, or a
        # tier conflict behind either.
        harm = []
        if off:
            harm.append("off account(s): %s — refresh or re-pool, do not "
                        "re-login" % ", ".join(sorted(off)))
        if dead:
            harm.append("unparseable file(s): %s — wired-but-dead slots the "
                        "proxy cannot draw on" % ", ".join(sorted(dead)))
        if unresolved:
            harm.append("row(s) with no readable or no unambiguous identity: "
                        "%s — counted on their own, so the total may be HIGH"
                        % ", ".join(unresolved))
        if conflicts:
            harm.append("tier CONFLICT, credited to no tier until resolved: "
                        "%s" % "; ".join(conflicts))
        return [(WARN, "intent-vs-actual: codex pool meets intent (%s) but "
                       "holds %s" % (observed, "; ".join(harm)))]
    return [(OK, "intent-vs-actual: codex credentials meet declared intent "
                   "(observed live: %s)" % observed)]


def check_trunk_authority():
    """Every repository a retip can run in, and whether it DECLARED its trunk.

    THE FEATURE THAT REQUIRES A DECLARATION SHIPPED WITHOUT ANYTHING THAT SAYS
    THE DECLARATION IS MISSING, and the two surfaces that depend on it degrade
    in OPPOSITE directions, which is why it went unnoticed for a day. A REVIEW
    retip computes work identity as the ordered per-commit patch-id sequence
    the tip adds to TRUNK; with no declared trunk that question is
    unanswerable, so it stamps `unverified` and PROCEEDS. A non-FF BUILD retip
    goes the other way and REFUSES, printing the LEGACY sentence about rows
    written before the binding — about rows minted today. One reads as a
    permissive shrug and the other as ordinary history, and neither says the
    word UNDECLARED.

    Measured 2026-08-25 (task/1510): the helm repository itself, where the
    feature landed, had neither key set. A review row was re-pointed at two
    commits of work its reviewer had never read, stamped `unverified`; with
    the authority declared the same call refuses in one line, naming the
    sequence that differs.

    THE POPULATION IS WHERE A RETIP CAN ACTUALLY HAPPEN — the distinct
    repo_ids the dispatch ledger names — not every project on the box, and it
    is read through `dispatches.repo_ids()`, which folds only each row's
    GENESIS event. A repository whose gitdir is gone is skipped rather than
    reported: it cannot host a retip either.

    THE COST IS TWO LOOPS AND THEY ARE NAMED SEPARATELY, because one sentence
    covering both is how a rung stops being measured. THE POPULATION READ
    SPAWNS NOTHING: `repo_ids()` folds each row's genesis event and never
    enters the carriage projection, which is the part that spawns one git per
    ledger ROW to decide a status this rung does not read. THE DECLARATION
    LOOP is per LIVE repository — a handful — never per project and never per
    ledger row. A rung whose cost sentence covers only its cheap half reads as
    cheap while the expensive half grows with the ledger.

    AND AN EMPTY POPULATION IS UNKNOWN, NEVER OK. A rung that reports all-clear
    over a set it failed to build is the vacuity this whole class is about."""
    from . import dispatches
    # THE LEDGER HAS ONE OWNER AND IT IS NOT THIS MODULE. My first cut called
    # `dispatches.ledger_rows()` — a name I invented, which does not exist —
    # and carried a hand-rolled JSON reader as its fallback. Two defects in one
    # shape: an accessor asserted rather than resolved, and a SECOND reader of
    # a store whose parsing rules live somewhere else. So the fix for the cost
    # is a NARROWER DOOR IN THAT OWNER, never a reader here: `repo_ids()` is
    # the same parser minus the projection this rung never reads, and it
    # returns the same UNAVAILABLE distinction `rows()` throws away.
    try:
        gitdirs, unavailable = dispatches.repo_ids()
    except Exception as e:                              # noqa: BLE001
        return [(WARN, "trunk authority: the dispatch ledger could not be read "
                       "(%s), so which repositories host retips is UNKNOWN"
                 % e.__class__.__name__)]
    if unavailable:
        return [(WARN, "trunk authority: the dispatch ledger could not be read "
                       "(%s), so which repositories host retips is UNKNOWN"
                 % unavailable)]
    live = [g for g in (gitdirs or ()) if os.path.isdir(g)]
    if not live:
        return [(WARN, "trunk authority: no readable repository in the "
                       "dispatch ledger — UNKNOWN rather than clear, because "
                       "an empty population proves nothing")]
    out, undeclared, broken = [], [], []
    for g in live:
        try:
            ref, remote, sha, failure = dispatches._declared_authority(g)[:4]
        except Exception as e:                          # noqa: BLE001
            broken.append((g, "%s reading the declaration" % e.__class__.__name__))
            continue
        if ref is None:
            undeclared.append(g)
        elif failure or not sha:
            broken.append((g, failure or "declared but no sha observed"))
    for g in undeclared:
        out.append((WARN, "trunk authority UNDECLARED in %s — retip cannot "
                          "compute work identity there, so review retips stamp "
                          "`unverified` and PROCEED while non-FF build retips "
                          "refuse as if the row predated the binding. Declare "
                          "it: git -C %s config helm.trunkRef refs/heads/<branch>"
                    % (g, g[:-5] if g.endswith("/.git") else g)))
    for g, why in broken:
        out.append((WARN, "trunk authority DECLARED BUT UNOBSERVABLE in %s: %s "
                          "— a contradiction rather than a gap, and retip "
                          "refuses on it" % (g, why)))
    if not out:
        out.append((OK, "trunk authority: declared and observable in all %d "
                        "repository(ies) the dispatch ledger names" % len(live)))
    return out


_COLLISIONS_DRILLDOWN = "`helm dispatch collisions` lists every one"


def check_dispatch_seq_collisions(census=None):
    """EVERY DISPATCH EVENT THE FOLD DROPPED BECAUSE ITS SEQ WAS TAKEN.

    A writer computes `seq` as its own fold's row seq plus one. A helm older
    than an event kind on the row cannot advance past that event, so the seq
    it writes is the one that event already holds; the append lands, its own
    self-check passes (it folds with the same vocabulary), and every current
    reader drops it. The author believes a decision was recorded and no reader
    honours it. `dispatches.seq_collisions` finds each one.

    WARN, NEVER FAIL: the ledger is append-only and every other row still
    reads correctly, so nothing here blocks work. What it owes is a person
    deciding, per dropped event, whether it must be recorded again with the
    trunk helm. `census` is the test seam — a function returning
    `seq_collisions()`'s answer.

    ONLY A LIVE ROW WARNS. A dropped event matters now while the row carrying
    it is open or held and no successor carries it: its verdict or cancel may
    be the answer the row is still waiting for. A row that has ended — closed,
    retired or superseded — owes nothing further, so its dropped event is
    HISTORY. History is counted on this row's line, never named, so a WARN that
    nobody could ever clear does not bury the one that matters. The count
    names `helm dispatch collisions`, which lists every one; both split on the
    same `ended` field of the same census. A collision that carries no `ended`
    answer reads live, because nothing proved its row ended.

    AN UNREADABLE LEDGER IS UNKNOWN, NEVER OK: a census over a record it could
    not read proves nothing about that record."""
    from . import dispatches
    try:
        found, unavailable = (census or dispatches.seq_collisions)()
    except Exception as e:                              # noqa: BLE001
        return [(WARN, "dispatch seq collisions: UNKNOWN — the census raised "
                       "%s" % e.__class__.__name__)]
    if unavailable:
        return [(WARN, "dispatch seq collisions: UNKNOWN — the dispatch "
                       "ledger could not be read (%s)" % unavailable)]
    if not found:
        return [(OK, "dispatch seq collisions: none — every event the fold "
                     "declined carries a seq no applied event holds")]
    live = [c for c in found if not c.get("ended")]
    history = len(found) - len(live)
    past = ("%d historical dropped event%s on rows that have ended (closed, "
            "retired or superseded) — %s"
            % (history, "" if history == 1 else "s", _COLLISIONS_DRILLDOWN))
    if not live:
        return [(OK, "dispatch seq collisions: none on a live row; " + past)]
    # EVERY LIVE DROPPED EVENT IS NAMED — the first of `_summary_names`' two
    # honest forms. Each one is a decision somebody believes was recorded on
    # a row still waiting for it, so each is a re-record owed now.
    rows = sorted({c["id"] for c in live})
    named = ", ".join(
        "%s (%s seq %d, reusing %s's)" % (
            _safe(c["id"][:12]), _safe(str(c.get("event") or "?")), c["seq"],
            _safe(str(c.get("reused") or "?")))
        for c in live)
    return [(WARN, "dispatch seq collisions: %d event%s on %d live row%s "
                   "(open or held, carried by no successor) reused a seq the "
                   "fold had already applied, so each was DROPPED — no reader "
                   "honours it: %s. Each writer's fold could not see the "
                   "event holding that seq, which is what a helm older than "
                   "the ledger does; decide for each whether it must be "
                   "recorded again, with the trunk helm%s"
             % (len(live), "" if len(live) == 1 else "s", len(rows),
                "" if len(rows) == 1 else "s", named,
                "; plus " + past if history else ""))]


def check_timers(census=None):
    """EVERY user timer, one question: is it RUNNING, not merely enabled?

    THE RUNGS ABOVE ARE PER-TIMER AND THAT IS WHY THIS EXISTS. chat-logflush,
    tasks-mirror and stale-bot each get a bespoke check asking its own question
    its own way, so a timer added after them starts with NO coverage and the
    absence looks exactly like a check that passes. This asks one question of
    all of them, so the next timer is covered by existing.

    IT REPORTS THE HEALTHY COUNT OUT LOUD. An empty finding list is the answer
    this rung gives most days, and on a box where the census could not run it
    would be the same empty list — so the OK line names how many timers were
    actually READ. A successful zero is valid on a fresh checkout.
    """
    from . import timerhealth
    try:
        rows, err = (census or timerhealth.census)()
    except Exception as exc:              # noqa: BLE001 — a rung that cannot
        return [(WARN, "timers: cannot tell (%s)"     # look says so, and never
                       % type(exc).__name__)]         # takes the report down
    if err:
        return [(WARN, "timers: %s" % err)]
    out = []
    for row in timerhealth.findings(rows):
        unit = row[0]
        if row[1] == timerhealth.TRAP:
            out.append((WARN,
                        "timers: %s is %s while its unit file says %s — THE "
                        "FIELD YOU WOULD CHECK SAYS YES. Two fleet watchdogs "
                        "sat in exactly this state for 19h. "
                        "`systemctl --user start %s`"
                        % (unit, row[2] or "not active", row[3], unit)))
        elif row[1] == timerhealth.FAILED:
            out.append((WARN,
                        "timers: %s has FAILED — it fires nothing, and its "
                        "unit file (%s) is the field that would otherwise "
                        "file this as somebody's decision. "
                        "`systemctl --user status %s`"
                        % (unit, row[3] or "unreadable", unit)))
        else:
            out.append((WARN,
                        "timers: %s is ACTIVE with NO next elapse while its "
                        "own schedule still promises one — armed and never "
                        "firing, which every is-active check reads as "
                        "healthy. `systemctl --user restart %s`"
                        % (unit, unit)))
    unknown = sorted(r[0] for r in rows if r[1] == timerhealth.UNKNOWN)
    if unknown:
        # NAME THE REMAINDER. A silent cut after three renders a census of
        # thirty holes identically to a census of three.
        more = ("" if len(unknown) <= 3
                else " and %d more" % (len(unknown) - 3))
        out.append((WARN, "timers: %d unit(s) could not be read (%s%s) — "
                          "UNMEASURED, not healthy"
                    % (len(unknown), ", ".join(unknown[:3]), more)))
    if not out:
        # EVERY POPULATION IS NAMED. "the rest are off" was a claim about
        # units this line never counted: a mid-trigger timer is neither
        # scheduled nor off, and folding it into the remainder let a census
        # of one RUNNING timer read as an all-clear about an idle box.
        kinds = (timerhealth.HEALTHY, timerhealth.RUNNING,
                 timerhealth.WAITING, timerhealth.SPENT, timerhealth.OFF)
        counts = dict((kind, 0) for kind in kinds)
        for row in rows:
            if row[1] in counts:
                counts[row[1]] += 1
        out.append((OK, "timers: %d of %d user timer(s) scheduled with a next "
                        "elapse; %d mid-trigger; %d event-waiting; %d spent; "
                        "%d explicitly off"
                    % (counts[timerhealth.HEALTHY], len(rows),
                       counts[timerhealth.RUNNING], counts[timerhealth.WAITING],
                       counts[timerhealth.SPENT], counts[timerhealth.OFF])))
    return out


def check_stop_timings(read=None):
    """THE LOG THE GUARD WROTE, READ IN THE SAME BREATH IT IS WRITTEN.

    The sibling instrument beside this one held 359 unread records for
    thirteen days while the question they answered stayed open. A writer
    without a reader is a log nobody opens, so this rung ships with the
    writer and not after it.
    """
    from . import stopprobe
    try:
        rows, err = (read or stopprobe.read)()
    except Exception as exc:              # noqa: BLE001 — a rung that cannot
        return [(WARN, "stop timings: cannot tell (%s)"   # look says so, and
                       % type(exc).__name__)]             # never takes the
    if err:                                               # report down
        return [(WARN, "stop timings: %s" % err)]
    out = []
    torn = stopprobe.malformed(rows)
    if torn:
        # OUR OWN TORN RECORDS. Dropping them renders a damaged log exactly
        # like a clean one, so they are named rather than skipped.
        out.append((WARN, "stop timings: %d record(s) this guard wrote are "
                          "MALFORMED (first missing: %s) — the log is damaged "
                          "and its counts are UNKNOWN, not clean"
                    % (len(torn), torn[0].get("missing", "unknown"))))
    # THE PIN THAT ADMITS A RUNG, READ BACK AGAINST WHAT THE RUNG SPENT.
    # `seats_stop_budget` gates each fat-tail rung on a FITTED cost, and
    # because that budget is cooperative the fit is the only thing that can
    # stop a slow rung from spending its successors' share. A fit nobody
    # re-measures does not decay gracefully — it silently disarms every rung
    # behind the one it under-states, and the guard still reports PASS. So the
    # constant is read back against this log on every run.
    #
    # ONLY THE FALSIFIED DIRECTION IS REPORTED, because only that direction
    # survives this population's censoring — see `stopprobe.overruns`. Silence
    # here is not a clean bill and this rung never renders it as one.
    from . import seats_stop_budget
    beat = stopprobe.overruns(rows, seats_stop_budget.ADMISSION_COST_S)
    if beat:
        out.append((WARN, "stop timings: %d fitted rung cost(s) FALSIFIED by "
                          "this log — %s. The fit is what ADMITS a rung, so an "
                          "under-stated one lets that rung spend the budget of "
                          "every rung after it while the stop still PASSES. "
                          "Re-pin ADMISSION_COST_S in "
                          "helm/seats_stop_budget.py."
                    % (len(beat),
                       "; ".join("%s spent %.3fs against a %.1fs fit (x%d)"
                                 % (rung, spent,
                                    seats_stop_budget.ADMISSION_COST_S[rung],
                                    count)
                                 for rung, (spent, count)
                                 in sorted(beat.items())))))
    unfinished, by_rung = stopprobe.findings(rows)
    if unfinished:
        worst = sorted(by_rung.items(), key=lambda kv: -kv[1])
        # WHAT THIS CAN AND CANNOT TELL YOU. An absent end means the end was
        # never OBSERVED — the run may have been killed, may still be going,
        # or may have failed to append its end. The rung does not know which,
        # and saying `died mid-rung` claimed the first as if it were measured.
        # AND WHEN THE LADDER BEGAN, because the total alone cannot separate
        # a slow ladder from one that started late in a process already busy
        # for a minute. Reported as a range over the runs that carry it, with
        # no threshold: naming a cutoff here would invent a number this rung
        # has no basis for.
        #
        # AND A MISSING FIELD IS UNAVAILABLE, NOT OLD. An earlier cut of this
        # sentence said the records "predate the field" -- a claim about
        # PROVENANCE that this rung cannot make, because a record written one
        # second ago omits the field just as completely when /proc could not be
        # read. Two different worlds, one observable, and the rung was naming
        # the comfortable one. It now reports the absence and says it cannot
        # tell which world produced it.
        began = [r["began_at_age"] for r in unfinished
                 if r.get("began_at_age") is not None]
        if began:
            when = ("; the ladder began %.1fs-%.1fs into its process"
                    % (min(began), max(began))) if len(began) > 1 else \
                   ("; the ladder began %.1fs into its process" % began[0])
            if len(began) < len(unfinished):
                when += (" (%d of %d carry it; for the rest the age is "
                         "UNAVAILABLE — predating the field and an unreadable "
                         "/proc are the same observable here)"
                         % (len(began), len(unfinished)))
        else:
            when = ("; no run carries a ladder-start age — a record omits it "
                    "when the field predates it AND when /proc could not be "
                    "read, and this rung cannot tell which")
        out.append((WARN, "stop timings: %d slow ladder(s) recorded no end — "
                          "last rung observed %s. An END is absent when a run "
                          "was KILLED, is STILL RUNNING, or failed to append "
                          "it; this rung cannot tell which.%s"
                    % (len(unfinished),
                       ", ".join("%s x%d" % (r, n) for r, n in worst[:3]),
                       when)))
    if out:
        return out
    # A ZERO HERE IS ONLY MEANINGFUL WITH ITS POPULATION: no slow ladders and
    # an unwired writer both read as nothing to report, so say how many
    # complete slow ladders were actually read.
    return [(OK, "stop timings: %d slow ladder(s) recorded, every one reached "
                 "its end" % len(stopprobe.runs(rows)))]


def check_injection_budget(survey=None):
    """WHAT SHARE OF EACH SEAT'S CONTEXT ITS OWN HOOKS PUT THERE.

    THE SUM IS THE MEASUREMENT NOBODY WAS TAKING. Every hook on the per-turn
    loop is individually small and individually justified, so no author of any
    one of them was ever wrong; the quantity that can go bad is the
    composition, and a composition with no surface grows for as long as nobody
    happens to look. This rung is that surface, and it names the worst seat on
    EVERY run rather than only on a breach -- a number that appears once it is
    already bad teaches no reader what normal looks like.

    ACROSS THE FLEET, AND REPORTED AS THE WORST SEAT RATHER THAN A MEAN. The
    shares differ by more between seats than they do between days, so a seat
    reading its own transcript would report a clean bill while a sibling sat
    at three times the budget, and an average over the fleet would bury that
    sibling under its quiet neighbours.

    IT READS ONE CONTEXT WINDOW PER SEAT, NOT ONE SESSION. Each census stops
    at the first compaction boundary going backwards, which is single-digit MB
    against transcripts that reach hundreds -- the whole fleet costs about a
    second. That is affordable for a health report and absurd on the per-turn
    path, which is why nothing here is wired to a hook.
    """
    from . import injectbudget
    try:
        rows = (survey or injectbudget.fleet)()
    except Exception as exc:              # noqa: BLE001 — a rung that cannot
        return [(WARN, "injection budget: census raised %s; the share is "
                       "UNMEASURED, not clean" % type(exc).__name__)]
    try:
        return injectbudget.fleet_findings(rows, condensed=True)
    except Exception as exc:              # noqa: BLE001 — same contract
        return [(WARN, "injection budget: verdict raised %s over %d measured "
                       "seat(s)" % (type(exc).__name__, len(rows)))]


def check_fixed_text(survey=None):
    """WHAT EACH CHECKOUT HANDS A SEAT BEFORE ITS FIRST TURN, and who shares
    it. The sibling of the rung above: that one reports what helm's hooks add
    per turn, this one the text that was already in the room. It names the
    heaviest checkout on every run and enforces nothing, because a ceiling on
    text several seats load is theirs to agree."""
    from . import fixedtext
    try:
        return (survey or fixedtext.findings)()
    except Exception as exc:              # noqa: BLE001 — a rung that cannot
        return [(WARN, "fixed text: the survey raised %s; the payload is "
                       "UNMEASURED, not small" % type(exc).__name__)]


def check_gitfacts_table():
    """The cross-process git-answer table, counted against its own ceiling.

    A BOUND WITH NO SURFACE IS STILL THE ROT CLASS this file guards for every
    other projection. `gitfacts` is content-addressed, so its growth is
    invisible by construction: one small file per answered question under a
    two-hex shard, and no single file that ever looks wrong. The count and the
    bytes are the only readings that show it at all, and `helm gc` prints a
    stream only once it is ALREADY over budget — so without this line a
    healthy table is silent exactly when an operator wants to know its ceiling
    is real, and a dead pruner looks the same as a quiet one.

    OVER THE CEILING IS NORMAL; TWICE IT IS NOT. The hourly sweep prunes back
    to the ceiling and the table refills between runs, so an ordinary pass
    finds it somewhere above the line — measured growth on this fleet is a few
    hundred entries an hour against a ceiling in the thousands. Twice the
    ceiling is a different claim, that the drain has not run for many hours,
    and it is the only reading here that WARNs.

    IT READS THE CEILING FROM THE STORE'S OWN MODULE, never from a number
    retyped beside it: `gitfacts.MAX_ENTRIES` is the same value `gc.POLICIES`
    declares as its count budget, so this line cannot drift from the thing it
    reports on."""
    from . import gitfacts
    try:
        found = gitfacts.entry_paths()
    except OSError as exc:
        return [(WARN, "gitfacts table unreadable (%s: %s) — its %d-entry "
                       "ceiling cannot be measured from here"
                 % (exc.__class__.__name__, exc, gitfacts.MAX_ENTRIES))]
    total = 0
    for path in found:
        try:
            total += os.path.getsize(path)
        except OSError:
            pass          # a concurrent prune won: it is not on disk to count
    line = ("gitfacts table: %d entr%s, %.1fKB of answers, ceiling %d entries"
            % (len(found), "y" if len(found) == 1 else "ies",
               total / 1024.0, gitfacts.MAX_ENTRIES))
    if len(found) >= 2 * gitfacts.MAX_ENTRIES:
        return [(WARN, line + " — twice its ceiling, so the retention sweep "
                              "is not running; `helm gc --apply` prunes it")]
    return [(OK, line)]


def check_chat_dir_debris():
    """Entries per room in the chat dir, against the budget the hook pays.

    EVERY TOOL CALL OF EVERY SEAT LISTS THIS DIRECTORY. The delivery hook asks
    `chat.list_rooms()`, which is one getdents over every entry, so the cost
    is the whole directory and not the rooms it returns. Measured on the
    live bus: 108,177 entries for 468 rooms, 0.28s a listing warm, delivery
    p95 1.5s against a 2s budget. Nothing about the rooms shows it; only this
    count does.

    TWO READINGS, EITHER WARNS. Per room is the ratio a healthy bus holds
    near two cursors per consumer; the total is what one listing costs. Idle
    meld rooms keep the ratio healthy while they multiply the total, so the
    ratio alone would miss the largest class. The WARN names the command for
    each class it can see."""
    from . import chatdebris
    try:
        c = chatdebris.census()
        idle = len(chatdebris.retirable_rooms())
    except OSError as exc:
        return [(WARN, "chat dir unlistable (%s) — its entry count is "
                       "UNMEASURED, not small" % type(exc).__name__)]
    per = c["entries"] / float(max(1, c["rooms"]))
    line = ("chat dir: %d entries for %d rooms (%.0f per room): %d cursors, "
            "%d cursor-sibling locks, %d meld rooms idle %d+ days"
            % (c["entries"], c["rooms"], per, c["cursors"],
               c["sibling_locks"], idle, chatdebris.IDLE_DAYS))
    if per <= chatdebris.PER_ROOM_WARN and \
            c["entries"] <= chatdebris.ENTRIES_WARN:
        return [(OK, line)]
    fixes = []
    if c["sibling_locks"] or c["cursors"]:
        fixes.append("`helm gc --apply` reaps the locks and the cursors of "
                     "sessions that are over")
    if idle:
        fixes.append("`helm chat retire-rooms --apply` archives the idle "
                     "meld rooms with their cursors")
    return [(WARN, line + " — over budget (%d per room, %d entries); every "
                          "delivery hook lists them all%s"
             % (chatdebris.PER_ROOM_WARN, chatdebris.ENTRIES_WARN,
                "; " + "; ".join(fixes) if fixes else ""))]


def check_burn_flags():
    """The burn-flag snapshot: fresh, covered, and not silently permanent.

    WARN-ONLY, ALL THREE RUNGS. Nothing here is a broken estate — a stale fold
    means the watchdog missed passes, a family with no money reader means one
    was never built, and a long RED with no expiry is owner debt. A FAIL would
    make a reporting gap look like a broken machine.
    """
    from . import burnflags
    out = []
    snap, age = None, None
    try:
        snap, age = burnflags.cached_snapshot()
    except Exception as exc:              # noqa: BLE001
        return [(WARN, "burn flags: the snapshot read raised %s — the reading "
                       "is UNMEASURED, not clean" % type(exc).__name__)]
    if not snap:
        out.append((WARN, "burn flags: no fresh snapshot (bound %ds). The "
                          "writer is `helm proxywatch --post`, not the bare "
                          "status read — check its timer; until it runs, "
                          "every family reads NOT MEASURED"
                    % burnflags.max_age_s()))
    else:
        out.append((OK, "burn flags: overall %s, folded %ds ago"
                    % ((snap.get("overall") or {}).get("colour"), age)))
    flags = (snap or {}).get("families") or {}
    blind = [f for f in burnflags.families() if f not in burnflags.MONEY_READERS]
    if blind:
        out.append((WARN, "burn flags: %d famil%s have NO money reader (%s), "
                          "so their budget axis is permanently GREY — that is "
                          "a reader nobody built, not a transient gap"
                    % (len(blind), "y" if len(blind) == 1 else "ies",
                       ", ".join(sorted(blind)))))
    now = time.time()
    for family in sorted(flags):
        flag = flags[family]
        if flag.get("colour") != burnflags.RED or flag.get("expires_at"):
            continue
        since = flag.get("measured_at")
        if since and (now - since) > 86400:
            out.append((WARN, "burn flags: %s has been RED for %dh with NO "
                              "expiry — a flag with no end is owner debt (a "
                              "reauth or a plan change), never a wait"
                        % (family, (now - since) // 3600)))
    if flags and not any(f.get("colour") == burnflags.GREEN
                         for f in flags.values()):
        out.append((OK, "burn flags: nothing reads GREEN — GREEN needs a full "
                        "account census AND an abundance reading, and no "
                        "abundance reader is wired on this host"))
    return out


def check_board_reads():
    """The owner's BOARD: did the land-pipeline read behind it succeed, as
    recorded by the process that took the reading (helm/boardread.py).

    THE DISTINCTION THIS CHECK IS FOR. "No board is being served here" and "the
    board is being served and its read is failing" are opposite findings, and
    silence alone cannot separate them. A host with no `helm web` has no
    board to break, so it reports UNKNOWN and no fault. A LIVE server whose
    last recorded read answered `unavailable` is a FAULT, because every card
    fanned out from that one read is rendering UNKNOWN on the owner's phone
    right now and nothing else on this fleet would ever say so.

    NEITHER HALF IS ASSERTED FROM ABSENCE. The fault needs a recorded failure
    AND a live observer; a failure whose observer is gone is a fact about the
    past, so it reports UNKNOWN about the present rather than a cure it did not
    witness. An unreadable record accuses the record, never the board."""
    from . import boardread
    st = boardread.state()
    where = "pid %s" % st["pid"]
    if not st["readable"]:
        return [(WARN, "owner board reads: the record at %s does not parse — "
                       "whether the board can be read is UNKNOWN, not fine"
                 % st["path"])]
    if not st["recorded"]:
        return [(OK, "owner board reads: UNKNOWN — no `helm web` on this host "
                     "has recorded a land-pipeline read. A host serving no "
                     "board has no board to break, so this is not a fault")]
    failed = "%d failed read%s recorded in total" % (
        st["failures"], "s"[:st["failures"] != 1]) \
        if isinstance(st["failures"], int) else "the failure total is UNKNOWN"
    if st["observer"] != "live":
        gone = ("that process is gone" if st["observer"] == "gone"
                else "whether that process still runs could not be read")
        if st["outcome"] == "failed":
            return [(WARN, "owner board reads: the last recorded land-pipeline "
                           "read FAILED %ds ago (%s) and %s — whether the board "
                           "reads NOW is UNKNOWN, and a live server is the only "
                           "thing that can answer it (%s)"
                     % (st["age_s"], st["reason"], gone, failed))]
        return [(OK, "owner board reads: UNKNOWN — no `helm web` is recording "
                     "reads here (%s read the board %ds ago and it answered, "
                     "then %s). A board nothing serves is not a fault (%s)"
                 % (where, st["age_s"], gone, failed))]
    if st["outcome"] == "failed":
        return [(FAIL, "owner BOARD UNREADABLE: the live `helm web` (%s) "
                       "answered its land-pipeline read %ds ago with "
                       "`unavailable` (%s) — every card the board fans out "
                       "from that one read renders UNKNOWN, so the owner's "
                       "home tab is blank where it is not wrong (%s). "
                       "`helm lr list` reads the same ledger from the CLI"
                 % (where, st["age_s"], st["reason"], failed))]
    if st["outcome"] == "warming":
        return [(OK, "owner board reads: the live `helm web` (%s) answered "
                     "WARMING %ds ago — a cold projection is rebuilding, which "
                     "is a named state and not a failed read (%s)"
                 % (where, st["age_s"], failed))]
    out = [(OK, "owner board reads: the live `helm web` (%s) read the land "
                "pipeline %ds ago and it answered (%s)"
            % (where, st["age_s"], failed))]
    if st["last_failed_age_s"] is not None:
        out.append((OK, "owner board reads: the last FAILED read was %ds ago "
                        "(%s) and the board has answered since"
                    % (st["last_failed_age_s"], st["last_failed_reason"])))
    return out


def check_web_servers():
    """How many `helm web` servers are running here, and from WHERE.

    THE COST THIS MAKES VISIBLE. A web server polls its own endpoints for as
    long as it lives, and one started against a lane worktree outlives the work
    it was started for — the lane lands, the agent moves on, the server keeps
    running. Until helm recorded its own servers nothing could see that. Three
    at once on an eight-core host has driven the load average past four times
    the core count, starving every seat on the box and timing out the hook that
    injects context into the owner's own turns. Nothing reported it; the servers
    were found only because he said the box felt slow.

    ONE SERVER IS THE EXPECTED SHAPE. The console is a single long-lived page
    the owner keeps open; a second server on the same host is serving nobody in
    almost every case, so the count itself is the finding and no judgement about
    which one is "real" is attempted here.

    ABSENCE IS NOT HEALTH AND IS NOT REPORTED AS IT. A host serving nothing and
    a registry that could not be read are different answers, and a stale record
    (a server killed before it could tidy up) is a third. Each is named."""
    from . import webserve
    st = webserve.live()
    out = []
    if st["unreadable"]:
        out.append((WARN, "web servers: %d record%s in %s do not parse — the "
                          "number of servers running here is UNKNOWN, not zero"
                    % (st["unreadable"], "s"[:st["unreadable"] != 1],
                       st["dir"])))
    n = st["count"]
    if not n:
        out.append((OK, "web servers: none recorded as running here. A host "
                        "serving no console is not a fault%s"
                    % (" (%d stale record%s cleared)"
                       % (st["stale"], "s"[:st["stale"] != 1])
                       if st["stale"] else "")))
        return out
    def one(x):
        # EVERY FIELD SAYS WHEN IT DOES NOT KNOW. A server found in the process
        # table but never registered has no recorded start and may not name its
        # port in argv, and inventing either would put a number in front of the
        # owner that nothing measured.
        port = "port %d" % x["port"] if x["port"] >= 0 else "port unknown"
        age = "up %dm" % (x["age_s"] // 60) if x["age_s"] is not None \
            else "start time unknown"
        tail = "" if x.get("registered", True) else ", UNREGISTERED"
        return "%s (pid %d, %s, %s%s)" % (port, x["pid"], age,
                                          x["cwd"] or "cwd unknown", tail)
    where = ", ".join(one(x) for x in st["servers"])
    if n == 1:
        out.append((OK, "web servers: one running — %s" % where))
        return out
    # THE REMEDY LINE IS FOR THE OWNER, who does not open a terminal. It says
    # what is wrong and what it costs him, and names the seats' job rather than
    # handing him a command he would have to run.
    out.append((FAIL, "web servers: %d are running on this host at once — %s. "
                      "Each one polls its own endpoints for as long as it "
                      "lives, so the extras are spending this box's cores on "
                      "pages nobody is reading, and that slows every seat "
                      "including the owner's own turns. Exactly one console is "
                      "wanted; whichever agent started the extras owes shutting "
                      "them down" % (n, where)))
    return out


def check_seat_physics_currency():
    """Seats running BEHIND their own configuration — the config-vs-RUNNING axis.

    THE OTHER CHECKS IN THIS FILE ALL READ FILES. A seat resolves its plugins,
    MCP servers and hooks ONCE, at session start, so a plugin enabled after a
    seat came up never reaches it — and every config-reading check sees the
    setting, sees the install, and reports healthy. That composition is what
    hid a live seat running three days behind its own settings until the owner
    noticed by hand (task/2885). helm/seatstale.py holds the measurement; this
    is the surface.

    IT WARNS AND DOES NOT FAIL, deliberately. A stale seat is a real defect
    with a real cost, and it is also the ORDINARY state of any host whose
    settings were touched today — so a FAIL would paint doctor red on a healthy
    estate for as long as the oldest seat lives, and a red that is always red
    steers nobody. The remedy is one sentence the owner can act on without a
    terminal, which is the whole point of surfacing it here.

    THE FAILED-READ ARM IS NOT THE CLEAN ARM. An unreadable census warns and
    names the input, because "no seat is behind" and "nothing could be measured"
    are opposite findings that silence collapses into one."""
    from . import seatstale
    st = seatstale.state()
    if not st["read"]:
        return [(WARN, "seat physics currency: %s — whether any seat is "
                       "running behind its own settings is UNKNOWN" % st["why"])]
    out = []
    if st["partial"]:
        out.append((WARN, "seat physics currency: the census could not certify "
                          "completeness, so the seats below are a FLOOR and "
                          "not the estate"))
    for row in st["stale"][:seatstale.NAME_CAP]:
        out.append((WARN,
                    "seat %s IS RUNNING ON OLD SETTINGS: it has been running "
                    "for %s, and its settings were changed %s after it "
                    "started. A seat only reads its plugins, tools and hooks "
                    "when it starts, so anything added since is missing from "
                    "it and nothing anyone tells it can put it there. THIS "
                    "SEAT NEEDS TO BE RESTARTED before it can use them."
                    % (row["seat"], seatstale.span(row["started_s"]),
                       seatstale.span(row["behind_s"]))))
    extra = len(st["stale"]) - seatstale.NAME_CAP
    if extra > 0:
        out.append((WARN, "%d further seat(s) are also running on settings "
                          "older than themselves and need the same restart; "
                          "the %d worst are named above"
                    % (extra, seatstale.NAME_CAP)))
    if st["unnamed_stale"]:
        out.append((WARN, "seat physics currency: %d further Claude "
                          "process(es) are older than the settings they "
                          "honour, and helm could not read a seat name for "
                          "them — they are counted here and NOT named, "
                          "because a pid is not something anyone can be asked "
                          "to restart" % st["unnamed_stale"]))
    for row in st["blind"][:seatstale.NAME_CAP]:
        out.append((WARN, "seat %s (pid %s): whether it is running behind its "
                          "own settings could not be decided — %s"
                    % (row["seat"] or "UNNAMED", row["pid"], row["why"])))
    blind_extra = len(st["blind"]) - seatstale.NAME_CAP
    if blind_extra > 0:
        out.append((WARN, "%d further seat(s) could not be decided either"
                    % blind_extra))
    if not st["stale"] and not st["unnamed_stale"]:
        out.append((OK, "seat physics currency: %d seat(s) are running the "
                        "settings that are on disk in their own home now"
                    % st["measured"]))
    return out


# THE DEPLOYED-ARTIFACT CANON. `fab gate` does not run out of a checkout: the
# fleet gates through scripts installed into a deploy directory that is not a
# git repository at all, so nothing there can be diffed, reviewed or rolled
# back by the tools every other file gets. When a deployed script stops
# matching the source it was installed from, the tree can no longer rebuild
# the thing that ran.
#
# WHAT THAT DOES AND DOES NOT MEAN, because the difference is the whole
# severity. It does NOT retroactively invalidate work already recorded: a
# receipt binds the RUN, not the binary that dispatched it. What it costs is
# forward: nobody can review, reproduce or roll back the artifact, and nobody
# can say when it started differing. That is worth a FAIL and it is not worth
# a claim about the past.
#
# THE DIRECTION IS THE FINDING, NOT THE MISMATCH. "drifted" leaves an operator
# with nothing to do. AHEAD (the deploy carries work the tree never took) is an
# unlanded change and the cure is to land it; BEHIND (the tree moved and the
# deploy did not) is a stale install and the cure is to deploy; DIVERGENT is a
# genuine conflict somebody must read before either. Three directions, three
# different repairs, so the rung names which one rather than making the reader
# rediscover it.
# WHICH deploy directory, and which project owns it, are this host's own
# names (`deploy-dir` and `deploy-project` in helm/localnames.py). A host that
# names none runs no deployed gate, and the rung owes nothing.
_DEPLOY_TREE = "fab/bin"
_DEPLOY_CHECK = "helm doctor (deployed artifact canon)"
_FOLD_SHOW = 6


def _fold(entries, drilldown, cap=_FOLD_SHOW):
    """A bounded join for entries whose detail must survive intact.

    `_summary_names` caps every item at a NAME's length, which would truncate a
    direction verdict into exactly the useless "it drifted" this rung exists to
    replace. Same honesty contract as that helper — render all, or name the
    executable surface that renders the rest — applied to longer items.
    """
    if len(entries) <= cap:
        return "; ".join(entries)
    return "%s; +%d more — %s" % ("; ".join(entries[:cap]),
                                  len(entries) - cap, drilldown)


def _drift_direction(canon, live):
    """(direction, detail) for one deployed artifact against its source.

    Bytes-differ is the PREMISE of this call, never its answer. Line-level
    added/removed counts are what separate the three cures. An artifact that
    does not decode as text is reported as undiffable rather than guessed at:
    a confidently wrong direction sends the reader to the wrong repair, which
    is worse than telling them the instrument could not say.

    THERE IS DELIBERATELY NO "BYTES DIFFER BUT NO LINE DOES" ARM. It reads
    like a real case and cannot occur: `splitlines(keepends=True)` is lossless
    and UTF-8 decoding is injective, so differing bytes always differ in some
    line. A dropped trailing newline is therefore a line change like any
    other, and reports as DIVERGENT rather than vanishing into +0/-0.
    """
    import difflib
    try:
        before = canon.decode("utf-8").splitlines(keepends=True)
        after = live.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return ("UNDIFFABLE", "not UTF-8 text: canon %d bytes, live %d bytes"
                % (len(canon), len(live)))
    added = removed = 0
    for line in difflib.unified_diff(before, after, n=0):
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    detail = "+%d/-%d lines against canon" % (added, removed)
    if not removed:
        return ("AHEAD", detail + "; the deploy carries work the tree never took")
    if not added:
        return ("BEHIND", detail + "; the tree moved and the deploy did not")
    return ("DIVERGENT", detail + "; both sides changed")


def check_deployed_artifact_canon(deploy_dir=None, project=None, repo=None):
    """Scripts running from the deploy directory equal their committed source.

    THE LIVE ARTIFACTS OBLIGE THIS CHECK, not the registry. A host with no
    deploy directory runs no deployed gate and owes nothing, so it reports
    nothing. A host that HAS one but cannot reach the canon is the LOUD case
    rather than the quiet one: something is gating right now against a source
    nobody can name, and silence there would be the same failure as the drift.
    """
    import hashlib
    from . import vcs

    from . import localnames
    dest = deploy_dir or localnames.value("deploy-dir")
    name = project or localnames.value("deploy-project")
    if not dest or not os.path.isdir(os.path.expanduser(dest)):
        return []
    dest = os.path.expanduser(dest)
    if repo is None and not name:
        return [(WARN, "deployed artifact canon: scripts are running from %s "
                 "but no project is named as their owner (`deploy-project` in "
                 "%s), so what gates this fleet was compared against nothing"
                 % (dest, localnames.CONFIG))]

    if repo is None:
        reg = pk.read_json(home.registry_path())
        projects = (reg.get("projects")
                    if isinstance(reg, dict) else None) or {}
        entry = projects.get(name)
        repo = entry.get("path") if isinstance(entry, dict) else None
    if not repo or not os.path.isdir(repo):
        return [(WARN, "deployed artifact canon: scripts are running from %s "
                 "but the '%s' checkout that owns them is not a registered "
                 "path, so what gates this fleet was compared against nothing"
                 % (dest, _safe(name)))]

    # THROUGH THE SEAM, NEVER A DIRECT SPAWN. helm/vcs.py is the sanctioned
    # spawn site; `run` is byte-preserving, which reading a blob requires, and
    # reports spawn trouble as rc -1 instead of raising, so an unreadable canon
    # becomes a WARN here rather than an exception that takes the whole report
    # down with it.
    git = vcs.backend(repo)
    rc, listing, _err = git.text(repo, "ls-tree", "HEAD:" + _DEPLOY_TREE)
    shipped = {}
    for line in listing.splitlines():
        meta, _tab, fname = line.partition("\t")
        bits = meta.split()
        if fname and len(bits) >= 2 and bits[1] == "blob":
            shipped[fname] = bits[0]
    if rc != 0 or not shipped:
        return [(WARN, "deployed artifact canon: could not enumerate HEAD:%s "
                 "in the '%s' checkout, so the scripts running from %s were "
                 "compared against nothing"
                 % (_DEPLOY_TREE, _safe(name), dest))]

    drift, modes, unreadable, missing, compared = [], [], [], [], 0
    for fname, mode in sorted(shipped.items()):
        live_path = os.path.join(dest, fname)
        if not os.path.isfile(live_path):
            missing.append(fname)
            continue
        try:
            with open(live_path, "rb") as handle:
                live = handle.read()
        except OSError:
            unreadable.append(fname)
            continue
        crc, canon, _e = git.run(repo, "show",
                                 "HEAD:%s/%s" % (_DEPLOY_TREE, fname))
        if crc != 0:
            unreadable.append(fname)
            continue
        compared += 1
        if live == canon:
            # EQUAL BYTES ARE NOT AN EQUAL ARTIFACT. A source committed
            # executable and deployed without the bit does not run at all, and
            # the reverse makes a plain file executable; both are invisible to
            # a digest.
            runnable = os.access(live_path, os.X_OK)
            if runnable != (mode == "100755"):
                modes.append("%s (canon %s, live %sexecutable)"
                             % (_safe(fname), mode,
                                "" if runnable else "not "))
            continue
        where, detail = _drift_direction(canon, live)
        drift.append("%s is %s (live %s, canon %s; %s)"
                     % (_safe(fname), where,
                        hashlib.sha256(live).hexdigest()[:16],
                        hashlib.sha256(canon).hexdigest()[:16], detail))

    try:
        present = sorted(f for f in os.listdir(dest)
                         if os.path.isfile(os.path.join(dest, f)))
    except OSError:
        present = []
    extra = [f for f in present if f not in shipped]

    rows = []
    if drift:
        rows.append((FAIL, "deployed artifact canon: %d of %d file(s) running "
                     "from %s are NOT their committed source in '%s' HEAD:%s, "
                     "so the tree cannot rebuild what is running — %s"
                     % (len(drift), compared, dest, _safe(name), _DEPLOY_TREE,
                        _fold(drift, _DEPLOY_CHECK))))
    if modes:
        # MODE-ONLY DRIFT IS REAL AND IT IS NOT THE SAME FINDING. The bytes
        # that ran ARE rebuildable from the tree, so this must not spend the
        # FAIL that says they are not. It still gets named: a source committed
        # non-executable and deployed executable means the deploy is editing
        # what it installs.
        rows.append((WARN, "deployed artifact canon: %d file(s) running from "
                     "%s match their committed BYTES but not their committed "
                     "MODE, so the deploy is changing what it installs: %s"
                     % (len(modes), dest, _fold(modes, _DEPLOY_CHECK))))
    if unreadable:
        rows.append((WARN, "deployed artifact canon: %d file(s) could not be "
                     "read on one side, so they were compared against nothing: "
                     "%s" % (len(unreadable),
                             _summary_names(unreadable, _DEPLOY_CHECK,
                                            _FOLD_SHOW))))
    if missing:
        rows.append((WARN, "deployed artifact canon: %d committed file(s) are "
                     "not deployed to %s at all: %s"
                     % (len(missing), dest,
                        _summary_names(missing, _DEPLOY_CHECK, _FOLD_SHOW))))
    if extra:
        rows.append((WARN, "deployed artifact canon: %d file(s) in %s are in "
                     "no committed source, so nothing reviews what they do: %s"
                     % (len(extra), dest,
                        _summary_names(extra, _DEPLOY_CHECK, _FOLD_SHOW))))
    if not drift and not modes and compared:
        rows.append((OK, "deployed artifact canon: %d file(s) running from %s "
                     "match '%s' HEAD:%s"
                     % (compared, dest, _safe(name), _DEPLOY_TREE)))
    return rows


CHECKS = ("check_home", "check_actuator_wiring", "check_harness_mirror",
          "check_authoring_disclosure",
          "check_authored", "check_projects", "check_adoption",
          "check_doc_task_conditionals",
          "check_projection_registry",
          "check_adopted_store", "check_lexicon_dead_vocabulary",
          "check_know_your_user", "check_cv", "check_filesystems",
          "check_seat_memory_ceilings",
          "check_inject_coverage", "check_guard_contract", "check_hook_scopes",
          "check_startup_doors",
          "check_env",
          "check_local_names",
          "check_physics_currency", "check_memory_base_honoured",
          "check_memory_base_sessions",
          "check_record", "check_resume_state",
          "check_chat_node", "check_chat_durability",
          "check_cred_families", "check_cred_drift", "check_skills_hub",
          "check_home_benefits",
          "check_git",
          "check_work_guard", "check_metaharness", "check_stale_bot",
          "check_keepalive_cadence", "check_cred_copy_staleness",
          "check_timers", "check_stop_timings", "check_injection_budget",
          "check_fixed_text",
          "check_gitfacts_table", "check_chat_dir_debris", "check_burn_flags",
          "check_board_reads", "check_web_servers",
          "check_seat_physics_currency",
          "check_trunk_authority", "check_dispatch_seq_collisions",
          "check_intent_actual",
          "check_deployed_artifact_canon")


def ensure_credentials():
    """All discovered authed homes, backup BEFORE unattended, holder-safe heal.

    Keep auth decisions and transactional writes in cred. Only counts leave
    this boundary: owner results and exception text can contain private paths.
    """
    from . import cred, hooks

    try:
        seats, unread = hooks.seat_homes()
    except Exception:
        return [(FAIL, "credential ensure: seat census raised an exception; backup and heal not run")]
    if unread:
        return [(FAIL, "credential ensure: seat census unreadable at %d locations; "
                       "backup and heal not run" % len(unread))]
    try:
        backups = cred.backup_all(apply=True)
        seen = {r["home"] for r in backups}
        for _seat, path in seats:
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            try:
                os.stat(os.path.join(real, cred.AUTH_JSON))
            except FileNotFoundError:
                continue          # a seat without Claude credentials has no pre-image
            backups.append(cred.backup(real, apply=True))
    except Exception:
        return [(FAIL, "credential ensure: backup raised an exception; heal not run")]
    failed = sum(not r["ok"] for r in backups)
    if failed:
        return [(FAIL, "credential ensure: backup failed for %d of %d homes; heal not run"
                 % (failed, len(backups)))]
    out = [(OK, "credential ensure: %d authenticated homes backed up or already current"
            % len(backups))]
    try:
        result = cred.heal(apply=True, hook=True)
    except Exception:
        return out + [(FAIL, "credential ensure: heal raised an exception; inspect "
                      "`helm cred heal` (read-only)")]
    plans = result["plans"]
    failed = sum(p["status"] != "restored" for p in plans)
    out.append((FAIL if failed else OK,
                "credential ensure: %d of %d drifted homes restored%s"
                % (len(plans) - failed, len(plans),
                   "; refusals or failures remain — inspect `helm cred heal` (read-only)"
                   if failed else "")))
    return out


def cmd_doctor(args):
    """doctor [--ensure] [--probe-memory] [--quiet] — health report; opt-in credential backup then heal; opt-in live memory-base probe."""
    from .cli import guard_tail

    rc = guard_tail("helm doctor", args,
                    flags=("--ensure", "--probe-memory", "--quiet"),
                    usage=cmd_doctor.__doc__)
    if rc is not None:
        return rc
    quiet = "--quiet" in args
    # THE WRITE MODE RUNS OUTSIDE THE SCOPE, DELIBERATELY. `--ensure` is the
    # only thing in this verb that mutates anything — cred backup and heal —
    # and projscope's law is that no write path may ever be answered from an
    # older question, so it keeps the unmemoised reads it has always had. The
    # scope opens after it and covers the CHECKS loop only, which is
    # report-only: every check returns [(level, message)] and none of them
    # writes.
    results = ensure_credentials() if "--ensure" in args else []
    # `--probe-memory` is the other write, for the same reason: it spends one
    # model call and records its result per Claude Code version, which the
    # static memory-base row in the loop below then reports.
    if "--probe-memory" in args:
        level, msg, version = probe_memory_live()
        record_memory_probe(level, msg, version)
        results.append((level, msg))
    # ONE PASS, ONE INSTANT — and the memo was inert here. It only caches
    # inside a scope and this verb opened none, so every memoised read in
    # every check computed: measured on the live fleet, 11,097 spawns of which
    # 1,793 were a distinct (cwd, argv), so 84% of them asked a question THIS
    # PROCESS had already answered. Holding one scope over the loop drops it
    # to 2,396 with the DISTINCT COUNT UNCHANGED — which is the proof that
    # nothing new was skipped — and the 86-row report byte-identical.
    #
    # THE CORRECTNESS ARGUMENT IS THE ONE projscope EXISTS FOR, not a speed
    # excuse. A doctor pass publishes ONE tally; two identical questions asked
    # forty seconds apart can answer differently, and the report would then
    # carry two instants under one line.
    with projscope.scope():
        results.extend(r for name in CHECKS for r in globals()[name]())
    for level, msg in results:
        if not quiet or level == FAIL:
            print("  %-4s %s" % (level, msg), file=sys.stderr if quiet else sys.stdout)
    tally = {lvl: sum(1 for l, _ in results if l == lvl) for lvl in (OK, WARN, FAIL)}
    if not quiet or tally[FAIL]:
        print("helm doctor: %d ok, %d warn, %d fail" % (tally[OK], tally[WARN], tally[FAIL]),
              file=sys.stderr if quiet else sys.stdout)
    if not tally[FAIL]:
        _record_genesis()
    return 1 if tally[FAIL] else 0
