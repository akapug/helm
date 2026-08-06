"""helm inject — the entries cluster: entry-line rendering, the WHO digest,
the codex-only nudge lines, and the persistent parsed-entry cache feeding
both lanes (load_entries/_lane_entries/_lanes).

Moved verbatim from the pre-split helm/inject.py.
"""
import hashlib
import json
import os

from .. import home, pk
from ._common import LINE_CAP, SA_FAMILIES, SA_LINES, WHO_CAP, _CACHE_VERSION


def _entry_line(e):
    """The line that FIRES. A gloss fires whole; only an ungloss'd entry is cut.

    LINE_CAP's own comment has said "the gloss fires, the full entry stays on
    disk" since it was written, and there was no gloss — `_entry_line_full` read
    `statement` and this truncated it. A truncation is not a gloss. It is a
    severed sentence: MEASURED 2026-07-30, three of the owner's five always-on
    rules fired at exactly 400 bytes, dropping 46-57% of each and ending
    mid-clause ("Reap stale testnet processes…"), while the other two could not
    fire at all because 3 x LINE_CAP == PINNED_BUDGET.

    So the owner's only way to make a rule fit was to REWRITE HIS OWN CANON
    shorter — trading the durable record for the firing line. With a gloss he
    keeps both: the full statement stays the record, the gloss is what reaches
    a seat every turn.

    An over-long GLOSS is still cut, deliberately: the budget is the budget, and
    silently honouring an oversized gloss would reintroduce the starvation this
    exists to end."""
    line = _entry_line_full(e)
    return line if len(line) <= LINE_CAP else line[:LINE_CAP - 1] + "…"


def _who_lines():
    """The WHO leg (know-your-user): operator digest off whoami.load_profile()
    — technical level + top guidance rendered as <=2 terse lines, jointly
    capped at WHO_CAP so the WHO digest never crowds the safety premises out of the
    pinned budget. guidance joins "; "-terse, so truncation keeps the TOP
    items (the list is owner-ordered). Fail-open: no profile / garbled /
    raising whoami -> [] (absent, never a blocked turn)."""
    try:
        from .. import whoami
        p = whoami.load_profile()
        raw = []
        if p["technical_level"]:
            raw.append("WHO operator: " + p["technical_level"])
        if p["guidance"]:
            raw.append("WHO guidance: " + "; ".join(p["guidance"]))
    except Exception:
        return []
    lines, left = [], WHO_CAP
    for line in raw:
        if left < 40:  # no room left for a meaningful line
            break
        if len(line) > left:
            line = line[:left - 1] + "…"
        lines.append(line)
        left -= len(line)
    return lines


def _sa_whisper():
    """The codex-only nudge lines — SA_LINES ((line, ledger-id) pairs, walk
    order) or (). Family is derived through the ONE existing resolver: the
    launch seam's HELM_CHAT_NAME (seat.py exports it; the same leg
    seats.derive_seat reads first) fed to seat._seat_family ('codex'/'codex-3'
    -> codex; anything outside seat.FAMILIES -> None) — never a second
    derivation. No seat name / non-codex family / any trouble -> () (fail-open,
    never a blocked turn)."""
    try:
        name = home.chat_name()
        if not name:
            return ()
        from .. import seat
        family, _err = seat._seat_family(str(name))
        return SA_LINES if family in SA_FAMILIES else ()
    except Exception:
        return ()


def _body(e):
    """The text that fires: the GLOSS when the entry has one, else the statement.

    One resolution, used by every type branch below, so a new type cannot
    quietly opt out of glossing — the way a second spelling of a mechanism
    always drifts from the first."""
    return (str(e.get("gloss") or "").strip()
            or e.get("statement") or "")


def _entry_line_full(e):
    # a provisional (xrev-cleared) entry FIRES like live but carries a visible
    # [provisional] PREFIX so the agent can weight it as not-yet-owner-ratified.
    # The prefix (not a suffix) survives LINE_CAP truncation — the tag can't be
    # the part that gets cut. candidates never reach here (load_all excludes them).
    pv = "[provisional] " if e.get("status") == "provisional" else ""
    t = e.get("type")
    if t == "prior":
        tag = "PREMISE" if e.get("class") == "certain" else "PRIOR %.2f" % e["confidence"]
        return "%s%s %s: %s" % (pv, tag, e["id"], _body(e))
    if t == "lexicon":
        return "%sTERM %s: %s" % (pv, e.get("term") or e["id"],
                                  str(e.get("gloss") or "").strip()
                                  or e.get("definition") or e.get("statement") or "")
    if t == "heuristic":
        return "%sMOVE %s: %s" % (pv, e["id"], _body(e))
    if t == "reference":
        return "%sREF %s: %s" % (pv, e["id"], _body(e))
    if t == "capability":
        # a LEVER, not a fact — the CAP prefix + the owner's exact
        # "you have <verb>: <what> (wired via <hook>, live)" body (e["statement"]).
        return "%sCAP %s" % (pv, _body(e))
    return "%s%s: %s" % (pv, e["id"], _body(e))


def _cache_file(project=None):
    """One cache file per physical-root set (the dirs are in the key), so a
    tmp-store test or a --project call never collides with the live store's."""
    from .. import store
    base = home.env("CACHE_DIR") or os.path.join(os.path.expanduser("~"), ".cache", "helm")
    key = hashlib.sha1(("%s|%r" % (project or "", store.roots(project))).encode()).hexdigest()[:12]
    return os.path.join(base, "store-cache-%s.json" % key)


def _store_sig(project=None):
    """Every store file's [path, mtime_ns, size] — the cache validity key.
    ~N stat() calls (~3-5ms on the live 880-file store) vs ~100ms of parse.
    Lists, not tuples, so equality survives the JSON round-trip."""
    from .. import store
    sig = []
    for root, _scope, d in store.roots(project):
        for _name, path in store._entry_files(root, d):
            try:
                st = os.stat(path)
            except OSError:
                continue
            sig.append([path, st.st_mtime_ns, st.st_size])
    sig.sort()
    return sig


def load_entries(project=None):
    """store.load_all(), ONCE, through the persistent parsed-entry cache.
    Any cache trouble (missing, stale, torn, unwritable) degrades to a direct
    parse — a cache is never worth failing a turn over."""
    from .. import store
    sig = _store_sig(project)
    path = _cache_file(project)
    try:
        with open(path, encoding="utf-8") as f:
            c = json.load(f)
        if c.get("v") == _CACHE_VERSION and c.get("sig") == sig:
            return c["entries"]
    except Exception:
        pass
    entries = store.load_all(project=project)
    try:
        pk.atomic_write(path, json.dumps(
            {"v": _CACHE_VERSION, "sig": sig, "entries": entries}, ensure_ascii=False))
    except Exception:
        pass
    return entries


def _lane_entries(project=None):
    """The store's parsed-entry cache PLUS the live capability self-index — the
    ONE entry list every JIT lane resolves against (gather + --explain). The
    capabilities are computed FRESH each turn (their live/absent probe is
    dynamic — an MCP wired mid-session must not be masked by a stale cache) and
    NEVER enter the persistent parsed-entry cache. Fail-open: a raising
    capability layer degrades to the store-only list, never a blocked turn."""
    entries = load_entries(project)
    try:
        from .. import capability
        caps = capability.live_entries(project)
    except Exception:
        return entries
    return entries + caps if caps else entries


def _lanes(text, project=None):
    """(pinned_entries, ALL ranked jit matches, the cached entry list) off ONE
    store parse — the cached list (store entries + the live capability index)
    feeds both lanes explicitly through the entries= seam. JIT comes back
    UNCAPPED so gather can both cap the lane and ledger the pre-cap candidate
    count; the raw list feeds the cooldown scorer and the coinage known-terms
    check without another parse. Capabilities ride the JIT resolver unmodified
    (they are load_class jit, so pinned() ignores them)."""
    from .. import store
    entries = _lane_entries(project)
    return (store.pinned(project=project, entries=entries),
            store.resolve_prompt(text, project=project, cap=len(entries),
                                 entries=entries),
            entries)
