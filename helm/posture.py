#!/usr/bin/env python3
"""helm posture — the artifact-door guard for a strategy on a dependency seam.

THE DEFECT (owner, 2026-08-22): an agent holding every fact for a strategy
still cannot assemble them until the owner supplies the framing question — and
the owner is not always there to supply it. The specimen: the integrator filed
task/1345 proposing a fork fix plus an upstream PR to Orca. The store held the
gate premise (upstream-only-what-is-proven-and-only-where-welcome: two gates
must pass before a PR is even considered) and the Orca ruling
(orca-accommodations-live-in-helm-never-fork-or-upstream); neither fired,
because keyword injection fires on VOCABULARY and a decision fires at a
MOMENT, and the two did not coincide. The advice-level cures live in the
skills; this is the DETERMINISTIC half at the door every such decision passes
through — `helm task add` and `helm dispatch send`.

THE PREDICATE (seam_hits): the body names an external DEPENDENCY and applies a
STRATEGY VERB to it — the two ADJACENT, at most one connector word between
("an orca rebuild", "patch dregg", "upstream PR to orca", "pin the cv"). Mere
co-occurrence anywhere in a body is NOT a hit: helm's whole history talks
about orca, dregg and cv beside words like pin and patch, so co-occurrence
would fire on most of it. MEASURED read-only over the live ledgers 2026-08-22
(2,428 task snapshots / 1,308 unique ids; 2,389 dispatch rows, whose bodies
are stored as a hash — only the 94 notes are text): the adjacency form hits
9 task ids in all, 6 once owner-asked rows are exempt, and 0 dispatch notes.
The generic words `upstream` and `fork` name a dependency only under a
SEAM-CHANGING verb (patch/rebuild/pin/vendor/wrap/shim/subtree — "rebuild the
fork", "patch upstream"): "upstream PR" on its own is the owner's ordinary
vocabulary and must not trip a guard — it is the exact phrase task/1342
carries, and that row must stay silent.

THE ESCAPES, both recorded: a `posture:` clause in the body carrying all three
labels (horizon, ownership, who-cares — case-insensitive, free text), or
`--posture-na REASON`, which the door writes into the row so a later reader
can see the question was asked and answered, not skipped.

OWNER ROWS ARE EXEMPT (`--owner-asked`, origin=owner): the three questions
interrogate an AGENT's assembled strategy; a row filed as the owner's own ask
records his words, and WHO-CARES is his to answer. task/1342 (owner-asked,
naming the same fork-plus-PR in passing) stays silent; task/1345 (agent) fires.

WHERE IT LIVES: the module-level doors — tasks.add, dispatches.send and
dispatches.add — so every caller passes it and the CLIs only forward the
escapes (dispatch fff5cef99aec: a guard at one door is a guard the
next caller walks around).

KNOWN LIMITS (review P2, predicate deliberately
unchanged — each is a census-measured trade, not an oversight):
  * NOUN FALSE POSITIVE — the verb list doubles as nouns, so "MEASURED from
    the orca fork source" (a noun phrase about a repository) fires exactly
    like "fork orca" (a strategy). task/1345 was caught on that phrase as
    well as on its real strategy line.
  * MODIFIER MISS — one connector word is the window, so "rebuild the whole
    orca binary" (two words between verb and dependency) is silent while
    "rebuild the orca binary" fires.
  * BOILERPLATE-LABEL BYPASS — `posture:` satisfaction is label presence,
    so "posture: horizon ownership who-cares" with no answers passes. The
    labels are the hook for a reader, not a proof of thought; a reviewer
    reads the clause.
"""
import re

# the named dependencies — the terrain's external seams (CLAUDE.md /dev table)
DEPENDENCIES = ("orca", "herdr", "dregg", "cv", "cliproxy", "claude code",
                "codex cli")
# generic seam words: a dependency ONLY under a seam-changing verb
GENERIC = ("upstream", "fork")
VERBS = ("patch", "fork", "upstream", "pr", "rebuild", "pin", "vendor",
         "wrap", "shim", "subtree")
SEAM_VERBS = ("patch", "rebuild", "pin", "vendor", "wrap", "shim", "subtree")
# the one word allowed between the verb and the dependency
CONNECTORS = frozenset(("a", "an", "the", "our", "its", "my", "their", "this",
                        "that", "of", "to", "into", "against", "for", "on",
                        "in", "at", "own"))
# the surface forms each verb token may take (a whitelist, never a stemmer:
# "wrapper" is a noun, "pinned lane" is helm's own vocabulary — both excluded
# by adjacency, but the forms stay explicit so the census is reproducible)
_FORMS = {
    "patch": ("patch", "patches", "patched", "patching"),
    "fork": ("fork", "forks", "forked", "forking"),
    "upstream": ("upstream", "upstreams", "upstreamed", "upstreaming"),
    "pr": ("pr", "prs"),
    "rebuild": ("rebuild", "rebuilds", "rebuilt", "rebuilding"),
    "pin": ("pin", "pins", "pinned", "pinning"),
    "vendor": ("vendor", "vendors", "vendored", "vendoring"),
    "wrap": ("wrap", "wraps", "wrapped", "wrapping"),
    "shim": ("shim", "shims", "shimmed", "shimming"),
    "subtree": ("subtree", "subtrees"),
}
_VERB_OF = {form: verb for verb, forms in _FORMS.items() for form in forms}
_DEP_FORMS = {
    "orca": ("orca",), "herdr": ("herdr",), "dregg": ("dregg",),
    "cv": ("cv", "clustervision"), "cliproxy": ("cliproxy", "cliproxyapi"),
    "claude code": ("claude", "code"), "codex cli": ("codex", "cli"),
    "upstream": ("upstream",), "fork": ("fork",),
}
_TOKEN = re.compile(r"[a-z0-9]+")

QUESTIONS = (
    ("HORIZON", "what does the next upgrade / reboot / crash / release do to this?"),
    ("OWNERSHIP", "who keeps this seam aligned, at what cadence, does that happen today?"),
    ("WHO-CARES", "does the other party want this from us?"),
)
LABELS = ("horizon", "ownership", "who-cares")
_LABEL_RE = {
    "horizon": re.compile(r"\bhorizon\b", re.I),
    "ownership": re.compile(r"\bownership\b", re.I),
    "who-cares": re.compile(r"\bwho[\s_-]?cares\b", re.I),
}
_POSTURE_RE = re.compile(r"\bposture\s*:", re.I)


def _tokens(text):
    return _TOKEN.findall(str(text or "").lower().replace("'s", ""))


def seam_hits(text):
    """The strategy-on-a-seam phrases in `text`, as "<verb> ~ <dependency>"
    strings — [] when the door may proceed without a posture.

    One pass over the word tokens: every dependency span and every verb token
    is located, then a hit is a (verb, dependency) pair whose spans do not
    overlap and sit adjacent with at most ONE connector word between, in
    either order. A generic seam word pairs only with SEAM_VERBS."""
    toks = _tokens(text)
    deps = []  # (start, end_exclusive, name)
    for name, forms in _DEP_FORMS.items():
        n = len(forms)
        for i in range(len(toks) - n + 1):
            if tuple(toks[i:i + n]) == forms:
                deps.append((i, i + n, name))
    verbs = [(i, _VERB_OF[t]) for i, t in enumerate(toks) if t in _VERB_OF]
    hits = []
    for vi, verb in verbs:
        for ds, de, name in deps:
            if ds <= vi < de:
                continue                      # the same token, not a pair
            if name in GENERIC and verb not in SEAM_VERBS:
                continue
            if vi < ds:
                gap = toks[vi + 1:ds]
            else:
                gap = toks[de:vi]
            if len(gap) > 1 or (gap and gap[0] not in CONNECTORS):
                continue
            lo, hi = min(vi, ds), max(vi + 1, de)
            phrase = " ".join(toks[lo:hi])
            hit = "%s ~ %s: %r" % (verb, name, phrase)
            if hit not in hits:
                hits.append(hit)
    return hits


def has_posture(text):
    """True when the body carries a `posture:` clause naming all three labels."""
    text = str(text or "")
    return bool(_POSTURE_RE.search(text)) \
        and all(rx.search(text) for rx in _LABEL_RE.values())


def refusal(verb, hits):
    """The door's refusal text: what tripped it, the three questions, the two
    escapes. `verb` is the door's own name for its usage line."""
    lines = ["%s: this names a strategy on a dependency seam — %s — and "
             "carries no posture. Answer the three questions in the body as a "
             "`posture:` clause (free text containing the labels horizon, "
             "ownership, who-cares), or pass --posture-na REASON to record "
             "why they do not apply:" % (verb, "; ".join(hits))]
    for label, q in QUESTIONS:
        lines.append("  %s: %s" % (label, q))
    return "\n".join(lines)


def check(verb, text, posture_na=None, owner=False):
    """-> None when the door may proceed, else the refusal text.

    `owner` = the row is the owner's own ask (task add --owner-asked); exempt.
    `posture_na` = the recorded reason the questions do not apply; a blank
    reason is NOT a reason and does not disarm the guard."""
    if owner:
        return None
    hits = seam_hits(text)
    if not hits:
        return None
    if str(posture_na or "").strip():
        return None
    if has_posture(text):
        return None
    return refusal(verb, hits)
