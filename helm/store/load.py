"""helm store — roots, parsers, and the load lane.

The physical-root set, the per-type fail-open parsers, and load_all() with its
shadowing law + the status/dedup projections (entries/candidates/reviewable/
counts/_find). Moved verbatim from the pre-split helm/store.py.
"""
import copy
import os
import re
import threading

from .. import home, pk, projscope
from ._common import (
    _slug, _coerce_conf, _is_pinned, _decode_lists, derive_class,
    derive_load_class, _scope_rank, _recency,
    STATUS_LIVE, STATUS_CANDIDATE, STATUS_PROVISIONAL, INJECTABLE_STATUSES,
    LEGACY_PREFIX, PRIOR_PREFIX, TYPE_SUBDIR, _SCAN_SUBDIRS, _TYPE_ORDER,
)


# ---------------------------------------------------------------------------
# roots — the injectable physical root set
# ---------------------------------------------------------------------------

def adopted_dir():
    """The live claude memory store helm adopts. HELM_ADOPTED_DIR (or legacy
    MELD_ADOPTED_DIR) overrides — that is how tests point it at a tmp dir."""
    return home.env("ADOPTED_DIR") or home.adopted_memory_dir()


# project -> (registry.json mtime, [claude memory dirs]); the mtime key keeps
# the registry load + dir stats off the per-turn hot path (20+ dirs otherwise).
_ADOPTED_PROJECT_CACHE = {}


def _project_adopted_dirs(project, projects=None):
    """The claude per-project memory dirs helm ADOPTS as project-scoped store
    roots: the project's canonical cwd + observed worktree cwds, collapsed to
    the one project (like sessions). Resolved from the registry, mtime-cached.
    Fail-open: no registry / unknown project / no memory dir -> [] (existing
    single-store behavior — every hermetic test that never seeds a registry is
    unaffected)."""
    if not project:
        return []
    if projects is not None:
        # Captured strict registry population: no mtime cache, no second read.
        rec = projects.get(project)
        return _adopted_dirs_for(rec, strict=True)
    try:
        mtime = os.path.getmtime(home.registry_path())
    except OSError:
        return []
    cached = _ADOPTED_PROJECT_CACHE.get(project)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        from .. import registry
        rec = registry.get(project)
    except Exception:
        rec = None
    dirs = _adopted_dirs_for(rec)
    _ADOPTED_PROJECT_CACHE[project] = (mtime, dirs)
    return dirs


def _adopted_dirs_for(rec, strict=False):
    dirs, seen = [], set()
    if rec:
        for cwd in [rec.get("path")] + list(rec.get("cwds") or []):
            if not cwd:
                continue
            d = home.claude_memory_dir_for(cwd)
            if strict:
                # Do not let isdir's false-on-error erase an adopted root. The
                # strict directory reader distinguishes absent from unreadable.
                exists = True
            else:
                exists = os.path.isdir(d)
            if d not in seen and exists:
                seen.add(d)
                dirs.append(d)
    return dirs


def roots(project=None, projects=None):
    """The ordered physical root set as (root, scope, dir) triples, WIDEST
    first — a later root SHADOWS an earlier one on a same-type same-slug
    collision, which is exactly the project > adopted-project > helm-global >
    adopted law. adopted-project is the project's OWN claude memory dir(s)
    (raw live store); it beats helm-global (project-specific raw over global)
    and is beaten by the authored ~/.helm/<name> layer. The same-slug shadow
    also dedups a byte-duplicated clone across two of a project's dirs."""
    out = [("adopted", "global", adopted_dir()),
           ("helm-global", "global", home.global_dir())]
    if project:
        adopted = _project_adopted_dirs(project, projects) if projects is not None else _project_adopted_dirs(project)
        for d in adopted:
            out.append(("adopted-project", "project:" + project, d))
        out.append(("project", "project:" + project, home.project_dir(project)))
    return out


# THE PROJECT AN ENTRY IS ABOUT — the fence that makes a project's inject lane
# about that project (task/2435). THE LAW: an entry carries the project it is
# about — a project name from the registry, or the value fleet for owner policy
# that applies everywhere. A seat receives only fleet entries plus entries of
# its own project.
#
# THE MEASURED PRODUCER THIS CLOSES, and why the fence belongs HERE and not at
# any renderer: roots() above returns ("helm-global", "global", global_dir())
# UNCONDITIONALLY ahead of every project root, load_all merges every file those
# roots hold, and NOTHING downstream ever compared an entry's scope to the
# requested project (resolve._jit_candidates and resolve.pinned filter on
# type/load_class only). So project=acme was fed the whole 641-file _global
# store — which is where helm's OWN seat-routing knowledge lives, because
# minting deliberately homes to _global without --project (cli.py:143-152:
# "MINTING is the verb where cwd would silently decide where knowledge lives").
# THE FAILURE MODE that closes: a premise about one project's seat routing —
# which seats a review may go to while the owner is away — fired into a turn in
# an unrelated project, as authoritative canon. roots() ADDS the project dir
# and subtracts nothing, so a correct project derivation never helped.
#
# The admission door is the one place a REQUESTING project meets an entry's
# RECORDED scope, so the law is one predicate read by load_all. A render-side
# filter would leave every other consumer of the same list leaking.
FLEET = "fleet"
# helm's own project — the one project a row derives when its own statement
# names an identity that exists nowhere else.
HOME_PROJECT = "helm"

# THE ACCEPTANCE SENTENCE THE DERIVATION IS BUILT TO: a non-helm seat's
# injection must contain no premise naming another project's LANES, TRAINS,
# TASKS or ROWS. It is NARROWER than "a global row is helm's until proven
# otherwise", and the difference was measured, one root at a time.
#
# A SEAT NAME IS NOT ONE OF THOSE, AND NEITHER IS A MODEL WORD. That premise is
# REFUTED: an earlier cut asked the seat authority which identities exist and
# scoped any statement naming one. Of the 283 rows it held to one project, 170
# were seat-name matches, and reading them found the overwhelming majority to be
# ATTRIBUTION carried inside general engineering rules — who measured a thing,
# whose output to check — which is provenance wearing a statement's clothes. So
# the authority is not an input to this derivation at all: only a POSITIVELY
# HELM ARTIFACT scopes a row, every ambiguous statement fails toward fleet, and
# the genuinely helm-only seat-routing premises get a RECORDED scope through
# `helm store rescope`, which is the door built for exactly that.
#
# THE ADOPTED ROOT IS NOT CLASSIFIED AT ALL. It holds the owner's own adopted
# canon — five of the nine rows every seat carries pinned. Hand-labelling it
# against a statement classifier found the classifier WRONG 8 OF 14 TIMES over
# 135 rows, and every error ran one way: it stripped fleet policy or owner canon
# from every non-helm seat. Only SIX rows are genuinely helm-scoped, so a
# content test buys six rows at the price of the owner's canon. The recorded
# field is the only way an adopted row becomes project-scoped, and
# `helm store rescope` is that door.
#
# THE HELM-GLOBAL ROOT KEEPS A CLASSIFIER because it holds 4770 rows and
# hand-labelling does not scale; the artifact rule marks about 2 PERCENT of them
# helm-specific. It FAILS TOWARD FLEET by construction: a row the rule cannot
# classify keeps reaching every seat rather than silently vanishing from all of
# them, because a canon row lost everywhere is the worse error and the adopted
# measurement is what proved which direction that is. Every token shape it
# matches is an ARTIFACT NAME — a thing that exists only inside one fleet's own
# rows — never an ACTOR name, which is what made the seat input fail: an actor
# can be cited by any rule about anything.
#
# WHY THE STATEMENT LINE ALONE decides, and never the body, keywords, source or
# rationale: those carry PROVENANCE. A rule learned while working one lane cites
# that lane in its source and is still general engineering canon. And the BARE
# PROJECT WORD in prose is not an identity either — "a helm guard refusing me is
# evidence about the world" is a rule about working under guards.


def _statement_line(e):
    """The entry's own STATEMENT — the statement, plus a heuristic's MOVE, which
    is where a heuristic says what to do. Nothing else: see the law above."""
    return "%s %s" % (e.get("statement") or "", e.get("move") or "")


# ARTIFACT shapes that exist only inside a fleet's own rows. Anchored or
# boundary-bound: an unanchored substring would let ordinary prose match.
_HELM_ARTIFACT = (
    re.compile(r"\blane/[a-z0-9]"),              # a lane room
    # HELM'S OWN lane WORKTREE ROOT, and only that one. EVERY fleet mints its
    # lanes at `<its repo root>-wt/<lane>` (helm.work._lanes builds that path),
    # so a pattern matching any `-wt/` read ANOTHER project's worktree path as
    # helm's: a general rule citing that project's own checkout got scoped to
    # helm and was withheld from the one project the path actually names. The
    # project half is derived from HOME_PROJECT — the same constant this module
    # RETURNS as the answer twenty lines below — so the pattern and the answer
    # cannot drift into two spellings.
    #
    # WHAT PROVES THE COMPONENT IS THE SLASH ON BOTH SIDES OF IT. Two earlier
    # shapes of this rule are REFUTED, and both failed the same way — they tried
    # to decide a path component by guessing what a NAME may contain:
    #
    #   A CHARACTER CLASS CANNOT EXPRESS A PATH COMPONENT. A negative lookbehind
    #   over [0-9A-Za-z_.~-] is an ASCII DENIAL LIST, and every character it
    #   forgot is a foreign basename that matches inside itself: a registered
    #   project caféhelm mints /work/caféhelm-wt/<lane>, é is outside the class,
    #   and that fleet's own rule about its own checkout was scoped to helm —
    #   the exact error the class was written to stop, one code point out. No
    #   enumeration closes this, because the set of characters a filesystem
    #   admits in a name is not the set an author can list.
    #
    #   A TOKENIZER GUESSES DELIMITERS THAT ARE DATA INSIDE A BASENAME. Splitting
    #   the text on whitespace, quotes or brackets first turns a registered
    #   project named `acme helm`, whose room is /work/acme helm-wt/<lane>, into
    #   a fresh token `helm-wt/<lane>` that then matches at its own start. A
    #   space in a basename is CONTENT, and so is a quote.
    #
    # So the rule tests the COMPONENT ITSELF and claims nothing about names: the
    # text names helm's worktree root when it contains `/helm-wt/` — a slash on
    # BOTH sides, which is what makes `helm-wt` a whole component — or when
    # `helm-wt/` stands at position 0 of the scanned statement, where the left
    # slash cannot exist. /abs/helm-wt/<lane>, ../helm-wt/<lane> and a statement
    # BEGINNING `helm-wt/<lane>` are helm's; /work/caféhelm-wt/<lane>,
    # /work/acme helm-wt/<lane>, /work/acme-helm-wt/<lane>, myhelm-wt/x,
    # helmet-wt/x, helm-wt-old/x and a mid-prose `see helm-wt/<lane>` are not.
    # That last one is the deliberate cost: a bare reference inside prose is
    # AMBIGUOUS, so it stays fleet under existing law and its author can scope it
    # explicitly with `helm store rescope`.
    re.compile(re.escape("/" + HOME_PROJECT + "-wt/")),
    re.compile(r"^" + re.escape(HOME_PROJECT + "-wt/")),
    re.compile(r"compose-train\b|\btrains?[ \-#]+\d"),   # a compose train
    re.compile(r"\btask/\d"),                    # a task row
    # A dispatch / land-request / receipt row id: 12 to 32 hex digits. The last
    # group of a UUID (8-4-4-4-12) has the same shape, and a UUID sits in every
    # Claude session scratch path, so a run that a dash joins to hex before it
    # is a UUID's group, never a row id (task/2547).
    re.compile(r"(?<![0-9a-f]-)\b[0-9a-f]{12,32}\b"),
    re.compile(r"\bhelm/[a-z0-9_]+[/.]"),        # a helm/ source path
)


def names_helm_artifact(text):
    """Does this text name an ARTIFACT that exists only inside one fleet's own
    rows — a lane room or HELM'S OWN lane worktree root, a compose train, a task
    row, a dispatch or land-request row id, or a source path under the package?

    ARTIFACTS ONLY, and never an actor: see the refuted seat-name premise in the
    law above. Anchored or boundary-bound throughout, so ordinary prose cannot
    match a fragment."""
    low = str(text or "").casefold()
    return any(rx.search(low) for rx in _HELM_ARTIFACT)


# how entry_scope reached its answer — the census needs the difference, the
# fence does not.
RECORDED = "recorded"       # the entry's own project field
ROOT_PROJECT = "root"       # written under a project root
ADOPTED_FLEET = "adopted"   # an unrecorded row in the adopted root
STATEMENT = "statement"     # an unrecorded helm-global row, classified


def entry_scope(e):
    """(project, how) — the project this entry is ABOUT, and how that was
    decided. `project` is a project name or FLEET; it is never None.

    A RECORDED project field ALWAYS WINS: it is the entry's own statement about
    itself, and `helm store rescope` is the door that writes it. Unrecorded, the
    root the entry was written under decides — a project root derives that
    project, the adopted root derives FLEET with no content test, and the
    helm-global root derives FLEET unless the statement line alone names a helm
    ARTIFACT, in which case it derives helm. A seat name and a model word scope
    nothing by themselves: the law above says why each of those is what it
    is."""
    own = str(e.get("project") or "").strip()
    if own:
        return own, RECORDED
    scope = str(e.get("scope") or "")
    if scope.startswith("project:"):
        derived = scope.split(":", 1)[1]
        if derived:
            return derived, ROOT_PROJECT
    if str(e.get("root") or "") == "adopted":
        return FLEET, ADOPTED_FLEET
    if names_helm_artifact(_statement_line(e)):
        return HOME_PROJECT, STATEMENT
    return FLEET, STATEMENT


def entry_project(e):
    """The project this entry is ABOUT: a project name or FLEET."""
    return entry_scope(e)[0]


def scope_label(e):
    """The scope a READER needs: the project that actually governs this entry's
    injection, and whether that was RECORDED or merely derived.

    THE STORAGE ROOT IS A DIFFERENT QUESTION AND MUST NOT STAND IN FOR THIS
    ONE. `helm store rescope` writes the entry's own `project` field;
    `entry_scope` reads that field FIRST and the root only as a fallback. A
    surface that renders `scope`/`root` — where the file SITS — therefore
    reports `global` for every entry in the global and adopted roots however it
    is classified: on the live store 255 of 255 entries carrying a recorded
    project sit in those roots, so rows recorded `helm` and rows recorded
    `fleet` render identically, and each is the opposite of its own record.

    The failure that costs is the read-back: record a scope, list the entry,
    see `global`, conclude the write never landed. A classification pass over
    several hundred entries is exactly the work that cannot proceed without a
    trustworthy read-back, so this is the label every entry-rendering surface
    owes.

    `(derived)` is not decoration — it is the difference between an entry
    somebody classified and one that is merely FALLING toward fleet because
    nothing is recorded about it. The backlog the census counts is precisely
    the set this marks."""
    owner, how = entry_scope(e)
    return owner if how == RECORDED else owner + " (derived)"


def injects_into(e, project, seat=False):
    """Does this entry belong in `project`'s lane? True when the entry is
    fleet policy, or when the entry is about exactly this project.

    WITH NO PROJECT THE TWO DOORS PART (task/2978). An inventory lens with no
    project asks for everything, and gets it. A SEAT with no project is a seat
    whose cwd no registered project claims (a scratch dir, /tmp, the home
    dir), and the owner's law for seats is "fleet entries plus entries of its
    own project": with no project of its own it receives fleet entries only.
    MEASURED on the E2 window: 23 of 4,333 inject ledger rows had no project,
    and on each of them every project's entries were eligible to fire."""
    if not project:
        return not seat or entry_project(e) == FLEET
    owner = entry_project(e)
    return owner == FLEET or owner == project


def scope_census(project=None):
    """Entry counts for `project`: fleet and own (both RECORDED), the two
    DERIVED buckets, and foreign.

    The derived split is the point. `unscoped_fleet` rows reach every seat only
    because nothing is recorded about them; `unscoped_helm` rows are held to one
    project only because a statement named a helm artifact. Both are numbers
    someone can work with `helm store rescope`, and neither is a silence."""
    fleet = own = unscoped_fleet = unscoped_helm = foreign = 0
    for e in _load_all_uncached(project):
        owner, how = entry_scope(e)
        if how == ADOPTED_FLEET:
            unscoped_fleet += 1
        elif how == STATEMENT:
            if owner == FLEET:
                unscoped_fleet += 1
            else:
                unscoped_helm += 1
        elif owner == FLEET:
            fleet += 1
        elif project and owner == project:
            own += 1
        else:
            foreign += 1
    return {"fleet": fleet, "own": own, "unscoped_fleet": unscoped_fleet,
            "unscoped_helm": unscoped_helm, "foreign": foreign}


def _default_dir(etype, project=None):
    base = home.project_dir(project) if project else home.global_dir()
    return os.path.join(base, TYPE_SUBDIR[etype])


# ---------------------------------------------------------------------------
# parsers — one per type, all fail-open (a garbled file is skipped, never fatal)
# ---------------------------------------------------------------------------

_PRIOR_DEFAULTS = {
    # a staged live-entry correction; absent means none. THE PARSER IS
    # THE THIRD ALLOWLIST — writer field list, parser defaults, decoder —
    # and a key missing from any one of them is dropped SILENTLY.
    "pending_revision": "",
    "id": "", "statement": "", "confidence": "", "load_class": "",
    "gloss": "",
    # the rule's PRECONDITIONS — a CSV of entry ids that ride with it when it
    # fires and share its probe vocabulary (task/1346; resolve._probes,
    # inject._entries._gate_plan). One default per type, the allowlist law.
    "gates": "",
    "status": "live", "domain": "", "keywords": "", "stated_ts": "",
    "last_updated": "", "source": "", "pin": "",
    # THE PROJECT THIS ENTRY IS ABOUT (task/2435): a registry project name, or
    # "fleet" for owner policy that applies everywhere. "" = not recorded.
    "project": "",
    "evidence_log": "", "confidence_history": "",
    "policy_kind": "", "policy_members": [], "policy_reason": "",
    "supersedes": "", "replaced_by": "", "source_prior": "",
    "retired_ts": "", "retired_why": "",
    # xrev-clear receipt (candidate -> provisional): who attested the
    # cross-family review + when. Carries through parse/rewrite for display.
    "xrev_by": "", "xrev_ts": "",
    # attestation pointer (premise.py annotates; parse + rewrite carry through).
    # attest_record is the NATIVE hash-chain record (the primary proof);
    # attest_anchor* is the OPTIONAL dregg anchor; attest_turn/attest_receipt/
    # attest_supersedes_turn are legacy (read-only, pre-native attestations).
    "attest_payload": "", "attest_ts": "", "attest_by": "",
    "attest_record": "", "attest_chain_index": "",
    "attest_supersedes_record": "", "attest_anchor": "", "attest_anchor_turn": "",
    "attest_turn": "", "attest_receipt": "", "attest_supersedes_turn": "",
}

_LEX_DEFAULTS = {
    # a staged live-entry correction; absent means none. THE PARSER IS
    # THE THIRD ALLOWLIST — writer field list, parser defaults, decoder —
    # and a key missing from any one of them is dropped SILENTLY.
    "pending_revision": "","term": "", "scope": "global", "definition": "", "kind": "",
                 "gloss": "", "gates": "",
                 "keywords": "", "domain": "", "source": "", "examples": [],
                 "updated_ts": "", "hits": "0", "status": "live",
                 "retired_ts": "", "retired_why": "",
                 "xrev_by": "", "xrev_ts": "",
                 # canon-as-controlled-language, Lane 1 (the synonym map). One
                 # canonical entry per concept; synonyms MAP to it rather than
                 # competing (docs/CANON_CONTROLLED_LANGUAGE.md §2). aliases =
                 # CSV of synonym TERMS a person/agent might say (linter +
                 # translator read this). alias_triggers = CSV of the
                 # word-boundary PROBE forms each alias contributes; the
                 # resolver folds these into THIS entry's probe set so a concept
                 # with synonyms keeps its full 1/df weight (resolve._probes).
                 # canonical = back-pointer on a STUB alias entry only (a
                 # synonym someone still `get`s by name); a stub does NOT compete
                 # as a resolve candidate (resolve._jit_candidates). Each field
                 # ships with a default ON PURPOSE: the parser only keeps keys
                 # that exist in the defaults, so a field lacking one is silently
                 # dropped on rewrite (the proven meme:true loss). New field ->
                 # new default, always.
                 "aliases": "", "alias_triggers": "", "canonical": "",
                 # THE PROJECT THIS ENTRY IS ABOUT (task/2435) — a registry
                 # project name, or "fleet" for owner policy that applies
                 # everywhere. Empty means "not recorded"; see entry_project.
                 "project": "",
                 # evidence receipts (#951): `evidence` resolves every type `get`
                 # resolves, and durable provenance rides IN the artifact (the
                 # journal is a lossy trail, per pk.event) — without this default
                 # the parser would silently drop a landed receipt on the next
                 # rewrite, the writer-emits-what-the-reader-drops loss again.
                 "evidence_log": ""}

_HEUR_DEFAULTS = {
    # a staged live-entry correction; absent means none. THE PARSER IS
    # THE THIRD ALLOWLIST — writer field list, parser defaults, decoder —
    # and a key missing from any one of them is dropped SILENTLY.
    "pending_revision": "",
    "id": "", "move": "", "statement": "", "trigger": "", "keywords": "",
    "gloss": "", "gates": "",
    "domain": "", "status": "live", "load_class": "jit",
    # THE PROJECT THIS ENTRY IS ABOUT (task/2435): a registry project name, or
    # "fleet" for owner policy that applies everywhere. "" = not recorded.
    "project": "",
    "stated_ts": "", "last_updated": "", "source": "",
    "supersedes": "", "replaced_by": "", "retired_ts": "", "retired_why": "",
    "xrev_by": "", "xrev_ts": "", "name": "", "description": "",
    "evidence_log": "",   # evidence receipts (#951) — see _LEX_DEFAULTS
}

_REF_DEFAULTS = {
    # a staged live-entry correction; absent means none. THE PARSER IS
    # THE THIRD ALLOWLIST — writer field list, parser defaults, decoder —
    # and a key missing from any one of them is dropped SILENTLY.
    "pending_revision": "",
    "id": "", "statement": "", "summary": "", "url": "", "keywords": "",
    "gloss": "", "gates": "",
    "domain": "", "status": "live", "load_class": "", "source": "",
    # THE PROJECT THIS ENTRY IS ABOUT (task/2435): a registry project name, or
    # "fleet" for owner policy that applies everywhere. "" = not recorded.
    "project": "",
    "stated_ts": "", "last_updated": "", "name": "", "description": "",
    "supersedes": "", "replaced_by": "", "retired_ts": "", "retired_why": "",
    "xrev_by": "", "xrev_ts": "",
    "evidence_log": "",   # evidence receipts (#951) — see _LEX_DEFAULTS
}

_EPISODIC_DEFAULTS = {"name": "", "description": "", "type": "", "load_class": ""}


class InvalidPriorError(ValueError):
    """Strict prior-shape rejection, distinct from frontmatter syntax."""

    REASON = "invalid-prior"
    MESSAGE = "approval policy population contains an unreadable prior"

    def __init__(self, path):
        super().__init__(self.MESSAGE)
        self.reason = self.REASON
        self.path = os.fsdecode(path)


def _parse_prior(path, strict=False):
    # Bulk-memory prior/prem files are historical inventory, not typed policy.
    # Read their identity on the SAME pass so strict policy capture can tell
    # valid bulk memory from an unreadable or malformed prior source.
    defaults = dict(_PRIOR_DEFAULTS, name="", description="") if strict else _PRIOR_DEFAULTS
    kwargs = {"strict": True} if strict else {}
    e = pk.parse_simple_frontmatter(
        path, defaults, list_keys=("policy_members",), **kwargs)
    if not (e and e["id"] and e["statement"]):
        if strict and (not e or e.get("policy_kind") or
                       not (e.get("name") or e.get("description"))):
            raise InvalidPriorError(path)
        return None
    _decode_lists(e)
    raw_conf = str(e.get("confidence") or "").strip()
    try:
        policy_confidence_valid = float(raw_conf) == 1.0
    except (TypeError, ValueError):
        policy_confidence_valid = False
    source = str(e.get("source") or "").strip()
    source_lower = source.casefold()
    policy_source_valid = bool(source) and "inferred" not in source_lower \
        and not source_lower.startswith("agent")
    conf = _coerce_conf(e.get("confidence"))
    pin = _is_pinned(e)
    e.update({"type": "prior", "confidence": conf, "class": derive_class(conf),
              "_policy_confidence_valid": policy_confidence_valid,
              "_policy_source_valid": policy_source_valid,
              "load_class": derive_load_class(e, conf, pin), "pinned": pin,
              "status": e.get("status") or STATUS_LIVE})
    return e


def _parse_lexicon(path):
    e = pk.parse_simple_frontmatter(path, _LEX_DEFAULTS, list_keys=("examples",))
    if not (e and e["term"] and e["definition"]):
        return None
    e["term_scope"] = e.pop("scope")  # authored scope; entry scope is root-derived
    status = e.get("status") or STATUS_LIVE  # a candidate lexicon is non-live
    kw = (e.get("keywords") or "").strip()
    # legacy mis-file: the add verb once filed the keywords CSV into kind: (a
    # real kind is a single taxonomy slug, never CSV) — those files must keep
    # resolving, unrewritten. The comma gate keeps taxonomy words ("phrase",
    # "bug-class") out of the probe vocabulary.
    kind = (e.get("kind") or "").strip()
    if not kw and "," in kind:
        kw = kind
        kind = "phrase"  # the CSV was never a taxonomy slug — any rewrite
        # (redefine/confirm/supersede) now persists the migrated shape instead
        # of carrying the mis-file forever
    e.update({"type": "lexicon", "id": e["term"], "statement": e["definition"],
              "confidence": 1.0, "class": "lexicon", "load_class": "jit",
              "status": status, "keywords": kw, "kind": kind,
              "domain": (e.get("domain") or "").strip(), "pinned": False,
              # synonym-map fields (Lane 1): normalized to stripped strings so
              # the resolver's probe fold + the stub-exclusion read a clean CSV
              "aliases": (e.get("aliases") or "").strip(),
              "alias_triggers": (e.get("alias_triggers") or "").strip(),
              "canonical": (e.get("canonical") or "").strip()})
    return e


def _parse_heuristic(path):
    e = pk.parse_simple_frontmatter(path, _HEUR_DEFAULTS)
    if not e or not e.get("id"):
        return None
    # move preferred, statement the priors-style alt key; description covers the
    # live store's hand-authored files that carry the move only in description.
    stmt = (e.get("move") or e.get("statement") or e.get("description") or "").strip()
    if not stmt:
        return None
    trig = (e.get("trigger") or e.get("keywords") or "").strip()
    e.update({"type": "heuristic", "move": stmt, "statement": stmt,
              "trigger": trig, "keywords": trig,
              "confidence": 1.0, "class": "heuristic", "load_class": "jit",
              "status": e.get("status") or STATUS_LIVE, "pinned": False})
    return e


def _parse_reference(path, name):
    e = pk.parse_simple_frontmatter(path, _REF_DEFAULTS)
    if not e:
        return None
    eid = (e.get("id") or "").strip()
    if not eid:
        nm = (e.get("name") or "").strip()
        eid = nm[4:] if nm.startswith("ref-") else (nm or name[4:-3])
    stmt = (e.get("statement") or e.get("summary") or e.get("description") or "").strip()
    if not (eid and stmt):
        return None
    lc = str(e.get("load_class") or "").strip().lower()
    e.update({"type": "reference", "id": eid, "statement": stmt,
              "confidence": 1.0, "class": "reference",
              "load_class": lc if lc in ("always", "jit", "dormant") else "jit",
              "status": e.get("status") or STATUS_LIVE, "pinned": False})
    return e


def _parse_episodic(path, name):
    e = pk.parse_simple_frontmatter(path, _EPISODIC_DEFAULTS)
    if not e:
        return None
    nm = (e.get("name") or "").strip()
    desc = (e.get("description") or "").strip()
    if not (nm or desc):
        return None  # no frontmatter identity (MEMORY.md-class) -> not an entry
    return {"type": "episodic", "id": nm or name[:-3], "statement": desc,
            "memory_type": (e.get("type") or "").strip(), "confidence": 1.0,
            "class": "episodic", "load_class": "dormant", "status": STATUS_LIVE,
            "keywords": "", "domain": "", "pinned": False}


def _parse_entry(path, name):
    """Filename-prefix dispatch (the drain/doctor classifier). reflex-*.md is
    reflex.py's store and MEMORY.md is the index — neither is an entry here.
    A typed-prefix file that fails its typed parse (the live store carries
    ~200 prem-/lex- named files that are really bulk memory: name+description
    only, no id/statement) FALLS BACK to episodic — visible in the inventory,
    never injected — where the predecessor's resolvers silently dropped it.

    ONE DECODE AT THE SINGLE EXIT, because this is the true convergence point:
    it dispatches to every typed parser and only _parse_prior decoded its own
    result. A live run measured what that cost — `helm store confirm`
    raised AttributeError on heuristic, reference and lexicon entries, whose
    pending_revision was still a str at write.py's `pending.get("statement")`.
    A sixth parser inherits the decode here instead of having to remember it,
    the same shape as _pending_lines on the write side.

    _parse_prior KEEPS its own call rather than handing it over: it has three
    callers outside this function (premise/_capture, store/cli, and a test),
    and moving the decode would have fixed three entry types by breaking three
    direct readers. _decode_lists is idempotent for exactly that reason."""
    if name == "MEMORY.md" or name.startswith("reflex-"):
        return None
    e = None
    if name.startswith((PRIOR_PREFIX, LEGACY_PREFIX)):
        e = _parse_prior(path)
        if e:
            e["_legacy"] = name.startswith(LEGACY_PREFIX)
    elif name.startswith("lex-"):
        e = _parse_lexicon(path)
    elif name.startswith("heuristic-"):
        e = _parse_heuristic(path)
    elif name.startswith("ref-"):
        e = _parse_reference(path, name)
    if not e:
        e = _parse_episodic(path, name)
    return _decode_lists(e) if e else e


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------

def _entry_files(root, d, strict=False):
    # adopted + adopted-project are flat claude memory dirs; helm roots nest
    dirs = [d] if root in ("adopted", "adopted-project") \
        else [os.path.join(d, s) for s in _SCAN_SUBDIRS]
    for sub in dirs:
        projscope.spend_or_raise("listing memory store directory")
        names = []
        try:
            with os.scandir(sub) as it:
                while True:
                    projscope.spend_or_raise("advancing memory store directory")
                    try:
                        entry = next(it)
                    except StopIteration:
                        break
                    names.append(entry.name)
        except FileNotFoundError:
            if strict and (os.path.lexists(sub) or os.path.islink(d)):
                raise
            continue
        except projscope.Expired:
            raise
        except Exception:
            if strict:
                raise
            continue
        projscope.spend_or_raise("sorting memory store directory")
        names.sort()
        projscope.spend_or_raise("sorting memory store directory")
        for n in names:
            projscope.spend_or_raise("examining memory store entry")
            p = os.path.join(sub, n)
            if strict and n.endswith(".md") and n.startswith((PRIOR_PREFIX, LEGACY_PREFIX)):
                # Do not let isfile's false-on-error hide an authority source.
                # The parser's strict open names unreadable/dangling entries.
                yield n, p
            elif n.endswith(".md") and os.path.isfile(p):
                yield n, p


def _load_root(root, scope, d, strict_policy=False):
    """One root -> {(type, slug): entry}, ALL statuses. Inside a root: prior-*
    always wins over a same-id legacy prem-* (dual-read migration law), and the
    narrowest authored lexicon scope wins per term (space: > project: > global).
    The live/retired filter is applied AFTER the cross-root merge (load_all) —
    filtering per-root let a retired narrow-scope entry vanish from the shadow
    map, resurrecting the stale wide-scope entry it shadowed."""
    out = {}
    files = _entry_files(root, d, strict=True) if strict_policy else _entry_files(root, d)
    for name, path in files:
        projscope.spend_or_raise("parsing memory store entry")
        if strict_policy:
            if not name.startswith((PRIOR_PREFIX, LEGACY_PREFIX)):
                continue
            e = _parse_prior(path, strict=True)
            if e:
                e["_legacy"] = name.startswith(LEGACY_PREFIX)
        else:
            e = _parse_entry(path, name)
        projscope.spend_or_raise("parsing memory store entry")
        if not e:
            continue
        e.update({"root": root, "scope": scope, "path": path})
        key = (e["type"], _slug(str(e["id"])))
        cur = out.get(key)
        if cur is not None:
            if e["type"] == "prior" and e.get("_legacy"):
                continue
            if e["type"] == "lexicon" and \
                    _scope_rank(e.get("term_scope")) >= _scope_rank(cur.get("term_scope")):
                continue
        out[key] = e
    projscope.spend_or_raise("finishing memory store root")
    return out



_ATTESTED_PRIOR_DEFAULTS = {
    "id": "", "statement": "", "attest_anchor_turn": "", "attest_turn": "",
}


def _attested_priors(project=None):
    """Shadow-resolved prior entries that carry a cave-turn pointer.

    This is the ledger's narrow read: four scalar frontmatter fields decide its
    entire answer. Parse only prior/legacy filenames and those four fields while
    retaining _load_root's exact prior-vs-legacy and cross-root shadowing laws.
    Every status remains eligible (a historical turn stays nameable after its
    premise retires); returned dicts are caller-owned."""
    merged = {}
    for root, scope, d in roots(project):
        out = {}
        for name, path in _entry_files(root, d):
            if not name.startswith((PRIOR_PREFIX, LEGACY_PREFIX)):
                continue
            e = pk.parse_simple_frontmatter(path, _ATTESTED_PRIOR_DEFAULTS)
            if not (e and e.get("id") and e.get("statement")):
                continue
            e.update({"type": "prior", "root": root, "scope": scope,
                      "path": path, "_legacy": name.startswith(LEGACY_PREFIX)})
            key = ("prior", _slug(str(e["id"])))
            if key in out and e["_legacy"]:
                continue
            out[key] = e
        merged.update(out)
    return _detached(e for e in merged.values()
                     if e.get("attest_anchor_turn") or e.get("attest_turn"))


_TLS = threading.local()    # per-THREAD .depth + .cache; see _scope_state()


def _scope_state():
    """This thread's scope depth and cache.

    PER-THREAD, NOT PER-PROCESS, and that distinction is the whole correctness
    of this cache. helm web is a ThreadingHTTPServer: two overlapping requests
    run in two threads inside one interpreter. A module-level dict and a class
    attribute are shared by both, so thread A's snapshot answered thread B's
    read (measured deterministically: a probe expecting
    {first, second} returned {first, first}), and A leaving its scope cleared
    the cache out from under B while B was still inside one.

    Nothing here is shared, so there is no lock and no contention: a thread can
    only ever see the reads it made itself, which is exactly what "scoped to
    ONE operation" was always supposed to mean."""
    tls = _TLS
    if not hasattr(tls, "depth"):
        tls.depth = 0
        tls.cache = {}
    return tls


def _detached(rows):
    """A caller-owned copy of a cached result. FULLY recursive, on purpose.

    `list(rows)` was NOT enough. It builds a new list around the SAME entry
    dicts, so a caller that mutates one entry mutates what the next read in the
    same scope sees (the first probe: the following read saw 99). The
    docstring said "a copy: callers mutate their result" and the code delivered
    a copy of the wrong thing.

    ONE LEVEL WAS NOT ENOUGH EITHER, and I argued otherwise before a probe
    measured it. I claimed list values were the only mutable ones and that a
    deepcopy "would spend the win". Both halves were false against the real
    corpus: 892 of 1,308 entries carry confidence_history / evidence_log lists
    whose ELEMENTS are dicts, and mutating one of those nested dicts poisoned
    the next cached read. The cost I was protecting turned out to be noise —
    deepcopy measured 12.98ms per copy against a 5.75ms one-level copy, inside
    an 11.5s projection.

    So the rule is now the simple one with no bound to get wrong: a caller owns
    everything it is handed, all the way down. A stated-but-false isolation
    bound is worse than an honest cost."""
    return copy.deepcopy(list(rows))


class read_scope:
    """Memoize whole-store reads for the duration of ONE operation.

    MEASURED 2026-07-31. `helm lr list` / `/api/lr` took 25-41s to project 312
    land loops, and the owner's console card renders UNKNOWN because its budget
    is 12s — so the land pipeline was unreadable on the surface built to show
    it. The profile put 30.7 of 41 seconds in ONE place: `approval_tier` calls
    `load_certain_policy` per row, which calls `load_all`, which re-walks and
    re-parses the ENTIRE store. 179 rows produced 716 root loads and 244,335
    frontmatter parses of the same 1,308 files.

    THE CACHE IS SCOPED TO AN OPERATION, NEVER TO TIME, and that is the whole
    design. A time-keyed or mtime-keyed cache would serve stale policy — and a
    long-lived process serving boot-time state is the exact failure class this
    fix exists inside (helm web served 40h-old code today; the owner console
    served an 8-day-old render). An in-place edit does not bump a directory
    mtime, so an mtime key would be silently wrong. Entering this scope starts
    an empty cache and LEAVING IT DROPS THE CACHE, so no read can ever outlive
    the operation that asked for it. Outside a scope nothing is cached and
    behaviour is byte-identical to before.

    Re-entrant on purpose: nested scopes share the outermost cache and only the
    outermost clears, so a caller need not know whether its callee also scopes.
    Re-entrancy is per-thread too — a nested scope shares the cache of the
    outer scope ON ITS OWN THREAD and is invisible to every other thread.
    """

    def __enter__(self):
        _scope_state().depth += 1
        return self

    def __exit__(self, *exc):
        t = _scope_state()
        t.depth -= 1
        if t.depth <= 0:
            t.depth = 0
            t.cache.clear()
        return False


def load_all(project=None, include_retired=False, include_dormant=True,
             types=None, scope_fence=False):
    """Every entry across the root set, enriched (id/type/statement/confidence/
    class/load_class/status/keywords/domain/scope/root/path/pinned), deduped by
    (type, slug) with the shadowing law: project > helm-global > adopted.

    Inside a `read_scope()` the result is memoized for that operation; outside
    one it is recomputed every call, exactly as before.

    scope_fence=True adds the project fence (injects_into, as a SEAT): only
    fleet entries and entries about `project` survive, and with no project
    only fleet entries do. It is a PARAMETER rather than the
    unconditional default because the two questions this function answers are
    different ones — an INVENTORY door (`store list`, `counts`, the lr census,
    a migration) must still see every row a root holds, or the unrecorded
    backlog becomes invisible to the very verbs that exist to work it, while a
    SEAT-FACING door (inject's lane composition) must see only what the seat's
    project is about. The law itself lives here, at the admission door, so no
    caller can spell it differently."""
    t = _scope_state()
    if t.depth > 0:
        key = (project, bool(include_retired), bool(include_dormant),
               tuple(types) if isinstance(types, (list, tuple)) else types,
               bool(scope_fence))
        hit = t.cache.get(key)
        if hit is not None:
            projscope.spend_or_raise("copying cached memory store")
            out = _detached(hit)
            projscope.spend_or_raise("copying cached memory store")
            return out
        out = _load_all_uncached(project, include_retired, include_dormant,
                                 types, scope_fence)
        t.cache[key] = out
        # DETACH ON THE MISS PATH TOO. Returning `out` handed the caller the
        # very list of dicts now sitting in the cache, so the FIRST caller
        # could poison every later read in the scope — the miss path was the
        # more dangerous of the two and the one the old code left open.
        projscope.spend_or_raise("copying loaded memory store")
        detached = _detached(out)
        projscope.spend_or_raise("copying loaded memory store")
        return detached
    return _load_all_uncached(project, include_retired, include_dormant,
                              types, scope_fence)


def _load_all_uncached(project=None, include_retired=False,
                       include_dormant=True, types=None, scope_fence=False):
    if isinstance(types, str):
        types = (types,)
    merged = {}
    projscope.spend_or_raise("enumerating memory store roots")
    store_roots = roots(project)
    projscope.spend_or_raise("enumerating memory store roots")
    for root, scope, d in store_roots:
        projscope.spend_or_raise("loading memory store root")
        merged.update(_load_root(root, scope, d))
        projscope.spend_or_raise("loading memory store root")
    projscope.spend_or_raise("linking memory store gates")
    link_gates(merged.values())
    projscope.spend_or_raise("linking memory store gates")
    out = []
    for e in merged.values():
        projscope.spend_or_raise("filtering memory store entry")
        # status filter AFTER the merge: a retired shadow-WINNER
        # drops out entirely — it must not un-bury the wider-scope entry it
        # shadowed (the record law survives scope precedence). live AND
        # provisional (xrev-cleared) both inject; candidate/retired stay out.
        if not include_retired and e.get("status") not in INJECTABLE_STATUSES:
            continue
        if not include_dormant and e.get("load_class") == "dormant":
            continue
        if types and e["type"] not in types:
            continue
        # THE PROJECT FENCE, in the same post-merge loop as status/dormant/type
        # and for the same reason: it must run AFTER the shadow merge, so a
        # project entry that shadowed a global one is judged on the WINNER's
        # recorded project rather than on whichever file happened to be read.
        # scope_fence IS the seat door, so a missing project means fleet only.
        if scope_fence and not injects_into(e, project, seat=True):
            continue
        out.append(e)
    projscope.spend_or_raise("finishing memory store load")
    return out


def gate_ids(e):
    """The entry's declared gates as a clean id list ([] when none). An id
    may carry its type — `lexicon:seam` — when the bare slug is shared
    across types (resolve_gate)."""
    return [g.strip() for g in str(e.get("gates") or "").split(",")
            if g.strip()]


def split_gate(gid):
    """'type:id' -> (type, slug); 'id' -> (None, slug). A prefix that is not
    a store type is part of the id, never a type. `premise:` reads as the
    prior type (a premise is a certain prior)."""
    head, sep, rest = str(gid or "").strip().partition(":")
    t = head.strip().lower()
    t = "prior" if t == "premise" else t
    if sep and rest and t in _TYPE_ORDER:
        return t, _slug(rest)
    return None, _slug(str(gid or ""))


def typed_id(e):
    """The TYPED spelling of an entry — `type:slug`, the string resolve_gate
    and every `type:id`-taking verb accept. Markers and remediation commands
    carry this, never the bare slug: a bare slug can name the wrong
    same-slug type (dispatch 0810a8fbe4e9).

    A SPELLING THAT READS BACK TO ITS OWN ROW (task/2980). _slug cuts at 60
    characters and strips an edge dash, so a slug whose cut lands on a dash
    is a key no reader can type: `prior:review-independence-is-a-different-
    model-and-no-reviewer-is-` re-slugs one character shorter and resolves to
    nothing (MEASURED on the live store: 26 rows, 23 live, spelled that way in
    every pointer, marker and alarm). Such a spelling runs on to the id's next
    non-dash character, and _slug of it is the row's key again."""
    raw = str(e.get("id") or "")
    key = _slug(raw)
    if key.endswith("-"):
        full = pk.slug(raw, cap=len(raw))
        n = len(key) + 1
        while n < len(full) and full[n - 1] == "-":
            n += 1
        key = full[:n]
    return "%s:%s" % (str(e.get("type") or ""), key)


# THE INJECTOR'S LINE TAGS, read as the types they render (inject._entries.
# _entry_line_full). The footer tells a seat to run `helm store get
# <type>:<id>` off the line in front of it, and the line says MOVE, TERM or
# REF where the store says heuristic, lexicon or reference. `premise:` and
# `prior:` already read as the prior type (split_gate).
LINE_TAGS = {"move": "heuristic", "term": "lexicon", "ref": "reference"}


def find_typed(eid, project=None, types=None):
    """_find that also accepts the typed spelling: `lexicon:seam` resolves
    exactly that type; a bare id keeps the typed-first law. Every verb that
    takes an entry operand (get / gates / gloss / keywords) reads through
    this so a remediation command printed by the injector is typeable — the
    injected line's own tag included (`move:<id>`, LINE_TAGS)."""
    head, sep, rest = str(eid or "").strip().partition(":")
    if sep and rest and head.strip().lower() in LINE_TAGS:
        eid = LINE_TAGS[head.strip().lower()] + ":" + rest
    want_t = split_gate(eid)[0]
    if want_t:
        if types and want_t not in types:
            return None
        # THE RAW REMAINDER, not split_gate's slug: _find slugs its operand,
        # and a slug slugged twice loses a trailing dash at the 60-character
        # cut, so `prior:<a 60-character id ending in a dash>` found nothing.
        return _find(rest, project=project, types=(want_t,))
    return _find(eid, project=project, types=types)


def gate_candidates(gid, entries):
    """Every entry in `entries` the gate id could mean, typed-first order.
    A typed id ('lexicon:seam') admits exactly its type; a bare one admits
    every type sharing the slug — the caller decides whether that is a
    resolution (resolve_gate: the first) or an ambiguity (regate: refuse)."""
    want_t, slug = split_gate(gid)
    hits = []
    for e in entries:
        projscope.spend_or_raise("resolving memory store gate")
        if _slug(str(e.get("id") or "")) == slug \
                and (want_t is None or e.get("type") == want_t):
            hits.append(e)
    projscope.spend_or_raise("sorting memory store gate candidates")
    hits.sort(key=lambda e: _TYPE_ORDER.index(e["type"])
              if e.get("type") in _TYPE_ORDER else len(_TYPE_ORDER))
    projscope.spend_or_raise("sorting memory store gate candidates")
    return hits


def resolve_gate(gid, entries):
    """THE ONE GATE RESOLVER — the entry a gate id names within `entries`,
    or None. The writer's validation (write.regate), the vocabulary fold
    (link_gates) and the injector's plan (inject._entries._gate_plan) all
    resolve through this, so the entry that validated is the entry that
    injects: a bare slug shared by a prior and a lexicon used to validate
    the prior (typed-first) and inject whichever the entry list yielded
    first (dispatch fff5cef99aec)."""
    hits = gate_candidates(gid, entries)
    return hits[0] if hits else None


def gate_key(e):
    """The identity an entry renders under — (type, slug), never the bare
    slug, so same-slug cross-type entries stay two entries."""
    return (str(e.get("type") or ""), _slug(str(e.get("id") or "")))


def link_gates(entries):
    """A RULE ARRIVES WITH ITS GATE — the vocabulary half (task/1346).

    For every entry naming `gates:`, and for every gate it names, each side
    gains `gate_probes`: the OTHER side's id plus keywords, as one CSV. The
    resolver folds that into the probe set (resolve._probes), so the rule's
    vocabulary pulls the gate exactly as strongly as the rule, and the gate's
    vocabulary pulls the rule. THE SPECIMEN: the gate premise "two gates must
    pass before a PR is considered" was keyed on upstream/pr/oss/culture and
    the Orca ruling on orca/fork/rebuild; an agent typing "upstream PR to orca"
    reached one and never the other. Linked, either phrase reaches both.

    Symmetric on purpose — a precondition is as much a reason to surface the
    rule as the rule is to surface the precondition. Only INJECTABLE partners
    link (a retired gate is a missing gate, and the injector says so); an id
    that resolves to nothing links nothing. Mutates in place; idempotent
    (a second pass recomputes the same CSVs). Entries must carry `id` and
    `keywords` (the loaded shape) — `gate_probes` is derived and never
    written back."""
    rows = []
    source = iter(entries)
    while True:
        projscope.spend_or_raise("copying memory store gate entry")
        try:
            rows.append(next(source))
        except StopIteration:
            break
    live = []
    for e in rows:
        projscope.spend_or_raise("filtering memory store gate population")
        if e.get("status") in INJECTABLE_STATUSES:
            live.append(e)
    pairs = {}   # id(e) -> set of partner ids (the probe donors)
    for e in live:
        projscope.spend_or_raise("linking memory store entry gates")
        for gid in gate_ids(e):
            projscope.spend_or_raise("linking memory store gate")
            g = resolve_gate(gid, live)   # the ONE resolver, injectable only
            if g is None or g is e:
                continue
            pairs.setdefault(id(e), {})[id(g)] = g
            pairs.setdefault(id(g), {})[id(e)] = e
    for e in rows:
        projscope.spend_or_raise("rendering memory store gate probes")
        donors = pairs.get(id(e))
        if not donors:
            e.pop("gate_probes", None)
            continue
        probes = []
        for d in donors.values():
            projscope.spend_or_raise("rendering memory store gate donor")
            for p in [str(d.get("id") or "")] + [
                    k.strip() for k in str(d.get("keywords") or "").split(",")]:
                projscope.spend_or_raise("rendering memory store gate probe")
                p = p.strip()
                if p and p not in probes:
                    probes.append(p)
        e["gate_probes"] = ",".join(probes)
    projscope.spend_or_raise("finishing memory store gate links")
    return rows


def entries(project=None):
    """The web/status projection alias — same list as load_all()."""
    return load_all(project=project)


def candidates(project=None, types=None):
    """The safe-inferred-capture tier: every status:candidate entry (excluded
    from every injecting lane by the load_all live-filter — the hard law). The
    surface `list --candidates` and coach's dup-search read here."""
    return [e for e in load_all(project=project, include_retired=True, types=types)
            if e.get("status") == STATUS_CANDIDATE]


def reviewable(project=None, types=None):
    """The owner review queue: candidate (fires NOTHING) + provisional (fires
    WITH a [provisional] tag) — the two non-ratified states the owner browses,
    approves (confirm -> live), or rejects (reject -> retired) in the web review
    panel. xrev-clear graduates candidate -> provisional between them. Newest
    capture first so the freshest inference is reviewed first."""
    rows = [e for e in load_all(project=project, include_retired=True, types=types)
            if e.get("status") in (STATUS_CANDIDATE, STATUS_PROVISIONAL)]
    rows.sort(key=_recency, reverse=True)
    return rows


def counts(project=None):
    """{root: {type: n}} per physical root (post intra-root dedup, retired
    included) — the doctor/status inventory view."""
    out = {}
    for root, scope, d in roots(project):
        per = {}
        for e in _load_root(root, scope, d).values():
            per[e["type"]] = per.get(e["type"], 0) + 1
        out[root] = per
    return out


_PREFIX_MIN = 8   # shortest typed prefix the statement-id fallback will honor


def _statement_id_matches(eid, rows, types=None):
    """Every STATEMENT-SHAPED entry (whitespace in the id) whose slug begins
    with the typed token, grouped {full-slug: {type: entry}}.

    THE DARK-ROW CLASS THIS SERVES (task/1077 defect 2, measured 2026-08-11:
    43 rows live): a mis-mint filed a whole statement as the id — e.g.
    "verify-branch-base-before-merge CANON (near-miss...)". The row's APPARENT
    id — the leading kebab token everyone reads, types, and cites — slugs to
    something its stored slug only STARTS WITH, so `get`/`keywords` answer
    not-found about an act-governing rule, and no CLI path re-keys it.

    Healthy kebab ids are NEVER prefix-matched: the fallback population is
    restricted to ids carrying whitespace, so no existing exact-id behavior
    can change, and an exact hit always wins before this is consulted."""
    want = _slug(str(eid or ""))
    if len(want) < _PREFIX_MIN:
        return {}
    grouped = {}
    for e in rows:
        if types and e["type"] not in types:
            continue
        full = str(e["id"])
        if any(ch.isspace() for ch in full) \
                and _slug(full).startswith(want + "-"):
            grouped.setdefault(_slug(full), {})[e["type"]] = e
    return grouped


def _find(eid, project=None, types=None):
    """The one entry an id resolves to: the shadowing-law winner, typed-first
    (prior > heuristic > reference > lexicon > episodic on a slug tie).

    A miss falls back to the statement-shaped-id prefix match — UNIQUE target
    only (see _statement_id_matches). Two statement-ids sharing the typed
    prefix stay not-found here rather than silently picking one: this helper's
    callers include WRITE verbs, and a guessed winner would put a lifecycle
    write on a record the reader never named. The CLI read surface lists the
    ambiguous candidates itself (cli.get) so the refusal teaches the cure.
    Identity is untouched on purpose — the stored id, its supersedes chain,
    evidence links and attestation hashes all keep their bytes; only the
    ADDRESS resolves, which is what makes the dark rows re-keyable at all."""
    want = _slug(str(eid or ""))
    hits = {}
    rows = load_all(project=project, include_retired=True)
    for e in rows:
        if _slug(str(e["id"])) == want and (not types or e["type"] in types):
            hits[e["type"]] = e
    if not hits:
        grouped = _statement_id_matches(eid, rows, types=types)
        if len(grouped) == 1:
            hits = next(iter(grouped.values()))
    for t in _TYPE_ORDER:
        if t in hits:
            return hits[t]
    return None


def _policy_hits(kind, project=None, strict=False, projects=None):
    """Every live prior declaring this policy kind. THE ONE SCAN.

    `policy_declared` and `load_certain_policy` must agree about what "exists"
    means or a caller can be told both "there is no policy" and "the policy is
    unusable" about the same store — which is two owners of one fact, the
    defect this project keeps finding in new places. They share this."""
    kind = str(kind or "").strip().casefold()
    if not kind:
        return []
    # CASEFOLD BOTH SIDES. Writing canonically is not enough on its own — every
    # row already on disk keeps whatever case it was written with, and a
    # read-only fix would let new mixed-case rows keep arriving. A review's
    # point: one place is not enough, in either direction.
    if strict:
        # One uncached source population, with the SAME per-root collision and
        # cross-root shadow law. Filter status only AFTER merge so a retired
        # narrower prior cannot resurrect the wide live declaration.
        merged = {}
        if projects is None:
            from .. import registry
            projects = registry.load(strict=True)["projects"]
        for root, scope, directory in roots(project, projects=projects):
            merged.update(_load_root(root, scope, directory, strict_policy=True))
        population = merged.values()
        if any(e.get("policy_kind") and e.get("status") not in
               ("live", "candidate", "provisional", "retired", "delete_eligible") for e in population):
            raise ValueError("declared policy has an unknown status")
    else:
        population = load_all(project=project, include_retired=True, types=("prior",))
    return [e for e in population
            if e.get("status") == STATUS_LIVE
            and str(e.get("policy_kind") or "").strip().casefold() == kind]


def policy_declared(kind, project=None):
    """Does ANY live prior declare this policy kind at all?

    ABSENT AND UNREADABLE ARE DIFFERENT ANSWERS, and `load_certain_policy`
    returns a sentence for both — "no live policy declares kind X" reads the
    same to a caller as "policy X lacks a human source". A land gate built on
    that string must either parse prose or conflate two states that demand
    OPPOSITE behaviour: no policy means nothing to enforce, an unreadable one
    means we cannot say. This answers only the first question."""
    try:
        return bool(_policy_hits(kind, project=project))
    except projscope.Expired:
        raise
    except Exception:                       # noqa: BLE001
        return True     # unreadable store: assume a policy EXISTS, so the
                        # caller lands in UNKNOWN rather than in "no tier"


def load_certain_policy(kind, project=None):
    """Return the one live certain prior declaring ``policy_kind == kind``.

    Policy is ordinary typed-store data under the existing root/shadow law, not
    a second registry. Any competing live declaration or incomplete declaration
    makes the policy unavailable rather than letting a caller guess authority.
    """
    kind = str(kind or "").strip()
    if not kind:
        return None, "policy kind is required"
    return _certain_policy_from_hits(kind, _policy_hits(kind, project=project))


def _certain_policy_from_hits(kind, hits):
    """Validate one already-read policy population, including proven absence."""
    if not hits:
        return None, "no live policy declares kind %s" % kind
    if len(hits) > 1:
        return None, "ambiguous policy kind %s: %s" % (
            kind, ", ".join(sorted(str(e["id"]) for e in hits)))
    policy = hits[0]
    if policy.get("class") != "certain" \
            or not policy.get("_policy_confidence_valid"):
        return None, "policy %s is not explicitly certain" % policy["id"]
    if not policy.get("_policy_source_valid"):
        return None, "policy %s lacks a human source" % policy["id"]
    members = policy.get("policy_members")
    if not isinstance(members, list) or not members:
        return None, "policy %s has no policy_members" % policy["id"]
    values = [kind, policy.get("policy_reason") or ""] + members
    if any(any(ord(c) < 32 or ord(c) == 127 for c in str(value))
           for value in values):
        return None, "policy %s contains control characters" % policy["id"]
    return policy, None
