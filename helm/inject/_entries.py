"""helm inject — the entries cluster: entry-line rendering, the WHO digest,
the codex-only nudge lines, and the persistent parsed-entry cache feeding
both lanes (load_entries/_lane_entries/_lanes).

Moved verbatim from the pre-split helm/inject.py.
"""
import fcntl
import hashlib
import json
import os
import re

from .. import home, pk
from ._common import (FIRST_SENTENCE_MIN, FOOTER, FOOTER_GATES, JIT_LINE_CAP,
                      LINE_CAP, SA_FAMILIES, SA_LINES, WHO_CAP,
                      _CACHE_VERSION)


def _entry_line(e, cap=None, short=False, hard=False):
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
    exists to end.

    A CUT LINE HAS A ROUTE TO THE REST. A bare ellipsis mid-word is the
    worst of both — long AND incomplete, with no way for the reader even to
    learn that the rest is one command away. A cut backs off to a word
    boundary and ends in an ellipsis; the LANE'S FOOTER (lane_footer) names
    the verb once, for every line in it (task/2980), rather than each cut line
    carrying `(helm store get <typed-id>)` of its own.

    `short` renders the statement's FIRST SENTENCE instead of the whole
    statement, under `cap`. That is the JIT lane's shape for an entry NOBODY
    HAS GLOSSED: a hit is a reminder and the store holds the record.

    A GLOSSED ENTRY IGNORES `cap` AND FIRES TO LINE_CAP, which is the promise
    at the top of this docstring made good rather than a second policy. The
    gloss is the one lever a writer has over what a seat receives; reducing it
    would take that lever away from exactly the authors who used it, and would
    make a lane tighter by punishing the work the gloss campaign is asking
    for. So the narrower cap reaches only lines nobody has shortened.

    `hard` is the one exception, and it belongs to the ARRIVAL budget
    (moments.POLICY), not to the renderer: a long-tail line on a turn over
    its cap is squeezed whole-lane, glossed or not, instead of a lower-ranked
    line being dropped (keyword rank carries no relevance, design 6.2). A
    routed line is never squeezed."""
    cap = LINE_CAP if cap is None else cap
    if str(e.get("gloss") or "").strip() and not hard:
        cap = max(cap, LINE_CAP)
    line = _entry_line_full(e, short=short)
    if len(line) <= cap:
        return line
    return _cut(line, cap, "%s:%s" % (e.get("type") or "",
                                      pk.slug(str(e.get("id") or ""))))


# A sentence ends at .!? followed by whitespace. Deliberately crude: the job is
# to stop severing a clause mid-word, not to parse English. A head shorter than
# FIRST_SENTENCE_MIN is an abbreviation, a version or an id, so the walk
# continues past it rather than returning a fragment.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s")


def _first_sentence(text):
    """The first real sentence of `text`, or all of it when it has only one."""
    text = (text or "").strip()
    for m in _SENTENCE_END.finditer(text):
        head = text[:m.start()].strip()
        if len(head) >= FIRST_SENTENCE_MIN:
            return head
    return text


def _cut(line, cap, key):
    """Cut `line` under `cap` at a word boundary, ending in an ellipsis.

    THE ROUTE IS THE FOOTER'S AND THE CUT STAYS PUT (task/2980). A route on
    the line itself, ` (helm store get <typed-id>)` charged to the cap, costs
    about 64 B on 63% of store lines and repeats the id the line's own prefix
    names (MEASURED, injection-quality eval e4); the lane's footer carries the
    verb once. The cut still reserves that route's width, spelled off the
    entry's `type:slug` key (never store.typed_id, whose spelling of a
    60-character slug ending in a dash runs on), so a cut line carries exactly
    the rule text it carried beside a route — none lost, none added — and the
    route's bytes leave the lane. How wide a cut line should be is the lane
    budget's question, not the renderer's.

    When the old route would have left no room for content, the line takes a
    plain cut to its full cap, as it always did."""
    room = cap - len(" (helm store get %s)" % key) - 1
    if room < FIRST_SENTENCE_MIN:
        return line[:cap - 1] + "…"
    cut = line[:room]
    sp = cut.rfind(" ")
    if sp > room // 2:
        cut = cut[:sp]
    return cut.rstrip().rstrip(",;:") + "…"


def _abridged(e, line):
    """Does `line` carry less than its entry's whole record? A cut, a gloss
    and a first sentence all do, and the footer is owed to each of them. A
    capability is not a store record (`helm store get` cannot read one), so it
    owes no footer."""
    if e is None or e.get("type") == "capability":
        return False
    return not line.endswith(_entry_line_full(dict(e, gloss=""), short=False))


def lane_footer(plan):
    """THE LANE'S ONE FOOTER, or None: FOOTER when any rendered store line is
    shorter than its entry, plus the gates the plan MENTIONED instead of
    rendering (FOOTER_GATES). One line per lane however many lines it covers,
    computed off the lane's own plan so gather and --explain cannot disagree
    about it.

    EVERY ID IT NAMES RESOLVES. A mention is a gate the store resolved among
    the live entries, spelled by store.typed_id, whose spelling `helm store
    get` reads back (a 60-character slug ending in a dash once printed a
    pointer to nothing; see typed_id). The COUNT is exact when the list is
    capped, the alarms' law."""
    from .. import inject as _inject
    owed = any(ok and kind in ("entry", "gate-earned", "gate")
               and _abridged(e, line)
               for kind, e, _r, _g, line, _lid, ok in plan)
    gates = [line for kind, *_mid, line, _lid, _ok in plan
             if kind == "gate-mention"]
    if not (owed or gates):
        return None
    if not gates:
        return FOOTER
    cap = _inject.PINNED_DROP_NAMES
    tail = "" if len(gates) <= cap else " (+%d more)" % (len(gates) - cap)
    return FOOTER + FOOTER_GATES + ", ".join(gates[:cap]) + tail


# ---------------------------------------------------------------------------
# a rule arrives with its gate (task/1346) — the delivery half
# ---------------------------------------------------------------------------

def _gate_line(rule_id, gate, cap=None, short=False, hard=False):
    """The rider line: the gate's own firing line, prefixed so a reader sees it
    is the gate of the rule ABOVE it and not an unrelated hit. A rider is
    rendered under its lane's own cap — a gate that fires wider than the rule
    it qualifies is the lane's budget leaking through its rider."""
    return "GATE of %s: %s" % (rule_id, _entry_line(gate, cap=cap, short=short,
                                                    hard=hard))


def _gate_dropped(rule_tid, gate_tid, why):
    """THE LOUD DROP. A gate the budget cannot carry is NAMED in the lane, with
    the command that fetches it — never omitted. A silent omission is the
    exact shape this mechanism exists to end: a rule that arrives alone and
    reads as complete. Both ids are the TYPED spelling (type:slug), the
    string every remediation verb accepts, so the command cannot land on a
    same-slug entry of another type."""
    return ("GATE of %s DROPPED (%s): %s — helm store get %s"
            % (rule_tid, why, gate_tid, gate_tid))


def _gate_missing(rule_tid, gate_id):
    return "GATE of %s MISSING from the store: %s" % (rule_tid, gate_id)


def _gate_cycle(rule_tid, gate_tid):
    """THE LOUD CYCLE. A link written before the writer refused cycles
    (write.regate) still sits on disk; the rule renders, this stands in
    for the rider that presupposes it back. Typed spellings throughout."""
    return ("GATE of %s CYCLE: %s names %s as its own gate — helm store gates "
            "%s --remove %s" % (rule_tid, gate_tid, rule_tid, gate_tid, rule_tid))


def pinned_admission(pinned_entries, entries, budget=None, who=None,
                     who_cap=None, drop_why="pinned budget"):
    """THE ONE rules-first pinned-lane computation. Every surface CONSUMES this.

    THREE builders computed this lane independently — gather, `--explain`, and
    store.pinned_stats — each carrying its own copy of the accounting. The cost
    is measured twice, in opposite directions, by two different authors:

      2026-07-30  pinned_stats started at used=0, a MODEL of the walk rather
                  than the walk, and reported 3 entries fitting where the live
                  walk fit 2. Cured by seeding the model with the WHO digest.
      2026-08-28  the ruling took WHO OUT of PINNED_BUDGET and moved it after
                  the rules. That seeding became an over-charge, so the same
                  surface again disagreed with the walk — and `--explain`, the
                  one surface whose entire job is to say what reaches the seat,
                  still rendered WHO first and charged.

    A model of a walk is wrong the moment the walk changes, and it is wrong
    SILENTLY because it keeps returning a plausible number. So this returns the
    admission record and the callers RENDER it; they may differ in presentation
    and must not differ in accounting, identity or suppression.

    RULES FIRST against the whole budget; WHO after, against WHO_CAP alone. A
    digest ABOUT the operator may never outrank a rule FROM him.
    """
    from .. import inject as _inject
    budget = _inject.PINNED_BUDGET if budget is None else budget
    who_cap = _inject.WHO_CAP if who_cap is None else who_cap
    plan = _gate_plan(pinned_entries, entries, base_budget=budget,
                      used=0, drop_why=drop_why)
    lines, ids, dropped, used = [], [], [], 0
    for kind, e, rule_id, gate_id, line, lid, ok in plan:
        if not ok:
            dropped.append(store_typed_id(e))
            continue
        lines.append(line)
        ids.append(lid)
        used += len(line)
    who = list(who or ())
    who_bytes = sum(len(l) for l in who)
    who_fits = bool(who) and who_bytes <= who_cap
    # THE FOOTER walks right after the rules it covers and is UNCHARGED, the
    # alarm's law: it is about the lines, not a rule competing with them.
    return {"plan": plan, "lines": lines, "ids": ids, "dropped": dropped,
            "used": used, "budget": budget, "footer": lane_footer(plan),
            "who": who if who_fits else [], "who_bytes": who_bytes,
            "who_fits": who_fits, "who_cap": who_cap}


def pinned_alarm(admitted, drop_names=None):
    """THE ONE loud-drop sentence, or None when nothing was dropped.

    gather and `--explain` each spelled this format string out in full. Two
    copies of one sentence is the same defect the admission record was built
    to end, one layer up: they had ALREADY drifted, because gather formatted
    the budget from the module global `_inject.PINNED_BUDGET` while --explain
    used `admitted["budget"]` — identical until anything patches or overrides
    the budget, at which point the two surfaces describe different lanes and
    both look right in isolation.

    Everything it says comes from the admission record, so the alarm cannot
    describe a walk other than the one that happened.

    The alarm is deliberately UNCHARGED to `used` (see gather): it is a
    diagnostic ABOUT the budget, not a rule competing for it, and charging it
    would let a full lane silence its own alarm. It is ONE line however many
    rules dropped, and the COUNT stays exact even when the id list is capped.
    """
    from .. import inject as _inject
    dropped = admitted["dropped"]
    if not dropped:
        return None
    cap = _inject.PINNED_DROP_NAMES if drop_names is None else drop_names
    shown = dropped[:cap]                    # TYPED ids, never bare slugs
    tail = "" if len(dropped) == len(shown) \
        else " (+%d more)" % (len(dropped) - len(shown))
    return ("[helm pinned] %d always-on rule(s) did NOT fit the %dB rule "
            "budget (%dB charged by %d delivered) and were NOT delivered: "
            "%s%s — gloss them shorter (helm store gloss <id> --set TEXT) "
            "or they stay dark"
            % (len(dropped), admitted["budget"], admitted["used"],
               len(admitted["ids"]), ", ".join(shown), tail))


def jit_alarm(dropped, budget, drop_names=None):
    """THE JIT LANE'S LOUD DROP, or None when the budget carried everything.

    Same law as pinned_alarm one function up, for the lane that had no budget
    at all until now: a hit the budget could not carry and a hit that never
    matched produced the same observable — absence. ONE line however many
    dropped, the COUNT exact even when the id list is capped, and the ids
    TYPED so `helm store get` lands on the entry that was withheld and not on
    a same-slug entry of another type."""
    from .. import inject as _inject
    if not dropped:
        return None
    cap = _inject.PINNED_DROP_NAMES if drop_names is None else drop_names
    shown = list(dropped)[:cap]
    tail = "" if len(dropped) == len(shown) \
        else " (+%d more)" % (len(dropped) - len(shown))
    return ("[helm jit] %d hit(s) did not fit the %dB turn budget: %s%s — "
            "read one with helm store get <id>"
            % (len(dropped), budget, ", ".join(shown), tail))


def store_typed_id(e):
    """The entry's TYPED id (type:slug) — never the bare slug.

    A bare slug is ambiguous across types: two entries of different types may
    share one slug, so a remediation line naming the bare id sends its reader
    to a `helm store` verb that cannot resolve it.
    """
    from .. import store
    try:
        return store.typed_id(e) if e is not None else "?"
    except Exception:
        return str((e or {}).get("id") or "?")


def _gate_plan(selected, entries, base_budget=None, rider_budget=None, used=0,
               drop_why="gate budget", line_cap=None, short=False,
               riders_for=None, squeeze=None):
    """THE ONE LANE MODEL: every line a lane renders and every line it says
    it could not, as items (kind, entry, rule_id, gate_id, line, lid,
    rendered), in render order. gather renders the items whose `rendered` is
    True; --explain prints the same items with their verdicts. Neither
    re-derives anything from text (dispatch 0810a8fbe4e9: two
    walks over one plan drifted — riders rendered for a rule the base budget
    had omitted, a gate without its rule).

    BASE FIRST. Which selected entries render as ENTRIES is a fixpoint: an
    entry steps aside for a carrier (another entry-rendering selected rule
    that names it as a gate) before or after it in rank; when two name each
    other the higher-ranked stays the entry; a side the fixpoint strands (the
    3-cycle) is rendered after all — nothing selected is erased. A gate that
    was itself selected renders ONCE, as the "gate-earned" rider directly
    under its rule: it keeps the slot it earned. The base walk is the greedy
    budget law: under `base_budget` (from `used`), the first line that does
    not fit ends the lane and every later base item is an unrendered entry
    (a JIT lane passes None: the cap-4 lines always render).

    RIDERS EXIST ONLY FOR RENDERED BASES — the rule a budget omitted
    contributes no rider and NO marker (a marker would itself be a gate
    without its rule). Riders follow every base line in rank order and spend
    `rider_budget` (a JIT allowance, GATE_BUDGET; for a pinned lane None =
    what base_budget has left): a rider past it is a "gate-dropped" marker,
    a gate the store cannot resolve is "missing", a non-selected gate naming
    its rule back is a "cycle" marker. Markers and their remediation commands
    carry TYPED ids (type:slug), the string resolve_gate and every verb accept.

    `line_cap` and `short` are the LANE'S rendering, threaded to every line
    this plan builds — entries and riders alike. They are arguments rather
    than a per-lane renderer because a lane whose riders render under a
    different cap than its entries is the same two-walks-one-plan drift the
    paragraph above records, one layer down.

    A RIDER RIDES ONLY ON A DETERMINISTIC LINE (task/2980). `riders_for` is
    the set of rule ids whose delivery is deterministic — a notice route; None
    means every rendered entry is (the pinned lane). A KEYWORD line's gates
    are not rendered: each becomes a "gate-mention" item, never rendered and
    never charged, whose `line` is the gate's typed id for the lane footer to
    name. Measured on the trigger-design panel (D8): riders on a keyword line
    tripled one block to 1,648 B, at a precision of about 1 in 8.
    A gate the store cannot resolve is still the loud "missing" marker.

    `squeeze`, (cap, rule ids), re-renders those KEYWORD lines (an entry, or
    a gate earned under one) at `cap` whether or not they are glossed: the
    arrival budget's shortening of a turn over its cap (inject._fit_jit).

    kinds: entry | gate-earned | gate | gate-dropped | gate-mention | missing
    | cycle."""
    from .. import store
    live = [e for e in entries or ()
            if e.get("status") in store.INJECTABLE_STATUSES]
    keys = [store.gate_key(e) for e in selected]
    earned = set(keys)

    def gates_of(e):
        return [store.resolve_gate(gid, live) for gid in store.gate_ids(e)]

    def names(r, key):
        return any(g is not None and store.gate_key(g) == key for g in gates_of(r))

    as_entry = list(keys)
    changed = True
    while changed:
        changed = False
        for i, key in enumerate(keys):
            if key not in as_entry:
                continue
            for j, rkey in enumerate(keys):
                if rkey == key or rkey not in as_entry or not names(selected[j], key):
                    continue
                if names(selected[i], rkey) and i < j:
                    continue            # mutual: the higher-ranked stays
                as_entry.remove(key)
                changed = True
                break
            if changed:
                break
    # the base candidates in render order: each entry, then its earned gates
    base, placed = [], set()

    def place(e, key):
        base.append(("entry", e, None, None))
        placed.add(key)
        for g in gates_of(e):
            if g is None:
                continue
            gkey = store.gate_key(g)
            if gkey in earned and gkey not in placed:
                base.append(("gate-earned", g, str(e["id"]), str(g["id"])))
                placed.add(gkey)

    for e, key in zip(selected, keys):
        if key in as_entry and key not in placed:
            place(e, key)
    for e, key in zip(selected, keys):
        if key not in placed:
            place(e, key)               # the no-erasure floor
    # THE BASE BUDGET WALK — greedy, first overflow ends the lane
    items, cut = [], False
    sq_cap, sq_ids = squeeze if squeeze else (None, frozenset())
    for kind, e, rule_id, gate_id in base:
        owner = str(e["id"]) if kind == "entry" else rule_id
        hard = owner in sq_ids
        cap = sq_cap if hard else line_cap
        line = (_entry_line(e, cap=cap, short=short, hard=hard)
                if kind == "entry"
                else _gate_line(rule_id, e, cap=cap, short=short, hard=hard))
        cut = cut or (base_budget is not None and used + len(line) > base_budget)
        if not cut:
            used += len(line)
        items.append((kind, e, rule_id, gate_id, line, str(e["id"]), not cut))
    rendered = {store.gate_key(it[1]) for it in items if it[6]}
    # RIDERS, for rendered ENTRIES only, after every base line
    allowance = rider_budget if rider_budget is not None \
        else (None if base_budget is None else base_budget - used)
    spent = 0
    for kind, e, rule_id, gate_id, _line, _lid, ok in list(items):
        if kind != "entry" or not ok:
            continue
        rkey = store.gate_key(e)
        rule_tid = store.typed_id(e)
        keyword = riders_for is not None and str(e["id"]) not in riders_for
        for gid, g in zip(store.gate_ids(e), gates_of(e)):
            if g is None:
                items.append(("missing", None, str(e["id"]), gid,
                              _gate_missing(rule_tid, gid), "gate-missing:" + gid, True))
                continue
            gkey = store.gate_key(g)
            if gkey in rendered:
                continue
            gate_tid = store.typed_id(g)
            if keyword:
                items.append(("gate-mention", g, str(e["id"]), gate_tid,
                              gate_tid, "gate-mention:" + gate_tid, False))
                rendered.add(gkey)
                continue
            if names(g, rkey):
                items.append(("cycle", None, str(e["id"]), gate_tid,
                              _gate_cycle(rule_tid, gate_tid),
                              "gate-cycle:" + gate_tid, True))
                rendered.add(gkey)
                continue
            line = _gate_line(str(e["id"]), g, cap=line_cap, short=short)
            if allowance is not None and spent + len(line) > allowance:
                items.append(("gate-dropped", g, str(e["id"]), gate_tid,
                              _gate_dropped(rule_tid, gate_tid, drop_why),
                              "gate-dropped:" + gate_tid, True))
            else:
                spent += len(line)
                # THE RIDER'S LEDGER ID IS TYPED, like the DROPPED id beside
                # it: a bare id collapsed same-slug riders of two types onto
                # one ledger row (dispatch 0fb12823851d)
                items.append(("gate", g, str(e["id"]), gate_tid, line,
                              gate_tid, True))
            rendered.add(gkey)
    return items


def _who_lines():
    """The WHO leg (know-your-user): operator digest off whoami.load_profile()
    — technical level + top guidance, <= 2 terse lines jointly capped at
    WHO_CAP. Fail-open: no profile / garbled / raising whoami -> [] (absent,
    never a blocked turn).

    WHOLE CLAUSES, NEVER A CUT WORD (trigger design section 3). Both fields
    are `; `-separated clauses, owner-ordered. The digest was cut mid-word at
    the cap ("runs a mul…", MEASURED by --explain) and its guidance,
    headline-first, never fit at all. Now the first guidance clause is
    reserved room (inline-summary-first is the rule a typed turn most needs),
    the level takes the whole clauses that fit around it, the guidance the
    whole clauses that fit after, so no line ends mid-clause. A single clause
    wider than its room is the one case still cut, at a word, with "…".
    The headline is reserved room only while it is short (half the cap).""" 
    try:
        from .. import whoami
        p = whoami.load_profile()
        level = _clauses(p["technical_level"])
        guide = [g.strip() for g in (p["guidance"] or ()) if str(g).strip()]
    except Exception:
        return []
    lead, glead = "WHO operator: ", "WHO guidance: "
    reserve = len(glead) + len(guide[0]) + 1 if guide else 0
    if reserve > WHO_CAP // 2:
        reserve = 0          # only a SHORT headline is worth starving the level
    lines = []
    if level:
        lines.append(_whole(lead, level, WHO_CAP - reserve))
    left = WHO_CAP - sum(len(l) for l in lines)
    if guide and left >= len(glead) + 24:
        lines.append(_whole(glead, guide, left))
    return [l for l in lines if l]


def _clauses(text):
    return [c.strip() for c in str(text or "").split("; ") if c.strip()]


def _whole(lead, clauses, room):
    """`lead` + as many whole clauses as fit in `room`."""
    out = lead
    for i, c in enumerate(clauses):
        piece = c if i == 0 else "; " + c
        if len(out) + len(piece) > room:
            break
        out += piece
    if out == lead:                       # the first clause alone overflows
        head = clauses[0][:max(room - len(lead) - 1, 0)]
        sp = head.rfind(" ")
        return (lead + (head[:sp] if sp > 0 else head).rstrip(",;:") + "…") \
            if head else ""
    return out


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


def _body(e, short=False, fallbacks=("statement",)):
    """The text that fires: the GLOSS when the entry has one, else the statement.

    One resolution, used by every type branch below, so a new type cannot
    quietly opt out of glossing — the way a second spelling of a mechanism
    always drifts from the first.

    A GLOSS IS ALREADY THE SHORT FIELD and is never reduced further: the
    author wrote it to be the firing line. `short` reduces only the FALLBACK —
    the raw statement — to its first sentence, which is the difference between
    an entry whose author shortened it and one nobody has glossed yet."""
    gloss = str(e.get("gloss") or "").strip()
    if gloss:
        return gloss
    for field in fallbacks:
        text = e.get(field) or ""
        if text:
            return _first_sentence(text) if short else text
    return ""


def _entry_line_full(e, short=False):
    # a provisional (xrev-cleared) entry FIRES like live but carries a visible
    # [provisional] PREFIX so the agent can weight it as not-yet-owner-ratified.
    # The prefix (not a suffix) survives LINE_CAP truncation — the tag can't be
    # the part that gets cut. candidates never reach here (load_all excludes them).
    pv = "[provisional] " if e.get("status") == "provisional" else ""
    t = e.get("type")
    if t == "prior":
        tag = "PREMISE" if e.get("class") == "certain" else "PRIOR %.2f" % e["confidence"]
        return "%s%s %s: %s" % (pv, tag, e["id"], _body(e, short))
    if t == "lexicon":
        # ONE resolver, not a second spelling of it: the lexicon's fallback
        # chain is definition-then-statement, and it reaches `short` through
        # the same door every other type does.
        return "%sTERM %s: %s" % (pv, e.get("term") or e["id"],
                                  _body(e, short, ("definition", "statement")))
    if t == "heuristic":
        return "%sMOVE %s: %s" % (pv, e["id"], _body(e, short))
    if t == "reference":
        return "%sREF %s: %s" % (pv, e["id"], _body(e, short))
    if t == "capability":
        # a LEVER, not a fact — the CAP prefix + the owner's exact
        # "you have <verb>: <what> (wired via <hook>, live)" body (e["statement"]).
        return "%sCAP %s" % (pv, _body(e, short))
    return "%s%s: %s" % (pv, e["id"], _body(e, short))


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
    # THE STORE FILES ARE THE WHOLE KEY, and that is a property of the
    # derivation rather than an economy: every input to an entry's scope now
    # lives in the entry's own file (its recorded project, the root it sits
    # under, its statement line). An earlier cut also stat'd the seat-name
    # authority, because a rename then changed which rows left the fleet lane
    # with no store file touched — that input is gone, so keying on it would
    # invalidate the cache on renames that cannot change an answer. _CACHE_VERSION
    # is what retires any cache written under the older derivations.
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
    """store.load_all() SCOPE-FENCED, ONCE, through the persistent parsed-entry
    cache — the seat-facing door, so the fence is asked for here (task/2435): a
    seat receives fleet entries plus entries about its own project, and nothing
    about another project. The cache file is keyed per root set and its version
    is bumped with the shape, so a pre-fence cache is never replayed.
    Any cache trouble (missing, stale, torn, unwritable) degrades to a direct
    parse — a cache is never worth failing a turn over."""
    from .. import store
    sig = _store_sig(project)
    path = _cache_file(project)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".lock", "a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
            with pk.open_regular(path, encoding="utf-8") as f:
                c = json.load(f)
        if c.get("v") == _CACHE_VERSION and c.get("sig") == sig:
            return c["entries"]
    except Exception:
        pass
    entries = store.load_all(project=project, scope_fence=True)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".lock", "a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            pk.atomic_write(path, json.dumps(
                {"v": _CACHE_VERSION, "sig": sig, "entries": entries},
                ensure_ascii=False))
    except Exception:
        pass
    return entries


def _df_key(cand):
    """A digest of EXACTLY the fields _df_map reads, in candidate order.

    _df_map's answer is a pure function of (type, id, keywords,
    alias_triggers, gate_probes) over the candidate set — nothing else is
    consulted — so this digest is the cache's whole validity argument and it
    is corpus-exact by construction. It deliberately does NOT key on the store
    signature: the candidate set is store entries PLUS the live capability
    self-index, which is computed fresh each turn and never enters that
    signature, so a store-sig key would serve a df map for a corpus the turn
    is not actually resolving against (the fast-path-drops-a-binding class).
    Digesting the inputs instead cannot disagree with them.
    """
    h = hashlib.sha1()
    for e in cand:
        h.update(("\x1f".join((
            str(e.get("type") or ""), str(e.get("id") or ""),
            str(e.get("keywords") or ""), str(e.get("alias_triggers") or ""),
            str(e.get("gate_probes") or ""))) + "\x1e").encode("utf-8"))
    return h.hexdigest()


def _df_cache_file(project=None):
    """Sibling of _cache_file, same per-root-set key — one df map per store."""
    base = home.env("CACHE_DIR") or os.path.join(
        os.path.expanduser("~"), ".cache", "helm")
    name = os.path.basename(_cache_file(project))
    return os.path.join(base, name.replace("store-cache-", "store-df-", 1))


def lane_df(entries, project=None):
    """_df_map over the turn's candidates, through a persistent cache.

    WHY THIS IS CACHED AT ALL. `helm inject` is a per-prompt hook and it is
    CPU-BOUND — measured on the live tree: 98% CPU, ~0.6s of CPU per turn,
    essentially no blocking IO. On a shared 8-core box its WALL time
    is therefore that CPU figure multiplied by how oversubscribed the box is,
    which is why the 10s `timeout` in the installed hook fires in BURSTS (every
    seat at once) rather than on any one slow seat: at load 34 the same work
    that costs 0.6s idle costs many seconds, and a seat that loses this hook
    loses its whole brief silently.

    _df_map was the largest single phase of that CPU — 0.209s per turn, 35% of
    construction — and it was paid TWICE per turn (resolve_prompt, then again
    for the cooldown scorer) to recompute an answer that only changes when the
    store does. It is a pure function of the candidate set, so it is cached on
    a digest of its own inputs (_df_key) and threaded through the turn.

    Fail-open exactly like the parsed-entry cache above: any cache trouble
    (missing, stale, torn, unwritable) degrades to the direct computation. A
    cache is never worth failing a turn over, and a WRONG df map would silently
    re-rank the JIT lane, so the key is the inputs themselves.
    """
    from .. import store
    cand = store._jit_candidates(entries)
    key = _df_key(cand)
    path = _df_cache_file(project)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".lock", "a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
            with pk.open_regular(path, encoding="utf-8") as f:
                c = json.load(f)
        if c.get("v") == _CACHE_VERSION and c.get("key") == key:
            return c["df"]
    except Exception:
        pass
    df = store._df_map(cand)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".lock", "a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            pk.atomic_write(path, json.dumps(
                {"v": _CACHE_VERSION, "key": key, "df": df},
                ensure_ascii=False))
    except Exception:
        pass
    return df


def _lane_entries(project=None):
    """The store's parsed-entry cache PLUS the live capability self-index — the
    ONE entry list every JIT lane resolves against (gather + --explain). The
    capabilities are computed FRESH each turn (their live/absent probe is
    dynamic — an MCP wired mid-session must not be masked by a stale cache) and
    NEVER enter the persistent parsed-entry cache. Fail-open: a raising
    capability layer degrades to the store-only list, never a blocked turn.

    NO MEMO HERE, AND THE ATTEMPT IS RECORDED SO IT IS NOT RETRIED BLIND.
    A probe measured that three surfaces (_lanes, _whisper,
    store.pinned_stats) each pay a full live_entries pass in one turn. I cached
    the result module-level, reasoning that per-process IS per-turn because
    every caller is a per-invocation hook. That is true in production and FALSE
    in the test suite, which is one process running 14,000 tests: the memo
    leaked a wired estate from one test into the next and gate:48bf36a0075715b6
    FAILED FIVE ARMS — three in test_capability, one in test_inject's who lane,
    and the memo's own must-miss arm.

    THE MEASURED TRADE, which is why this is reverted rather than re-engineered
    at this layer: the saving is 3 config reads down to 1, and ZERO for a
    pure-core estate because _probe_mcps_if_needed already short-circuits. That
    is not worth a cross-test capability leak. A correct version belongs INSIDE
    capability.live_entries with real turn-scoping, not in a module dict here —
    there is no turn token at this layer to key on, which is the actual reason
    the cheap version cannot be made safe.
    """
    entries = load_entries(project)
    try:
        from .. import capability
        caps = capability.live_entries(project)
    except Exception:
        return entries
    return entries + caps if caps else entries


def pinned_lane(project=None):
    """(always-entries, CORPUS) — THE ONE composition, for every surface that
    walks the pinned lane.

    The corpus is store entries PLUS the live capability self-index, and the
    always-set is filtered FROM that same list. Both halves must come from one
    call or they describe different worlds: pinned_stats composed its own with
    store.load_all() (no capabilities) while gather used this one, so a pinned
    rule whose GATE named a capability rendered its rider to the seat and read
    as a MISSING gate in stats. Handing the shared computation two corpora is
    the same defect as having two computations, one argument to the left.

    So no caller composes this itself — not gather, not stats, not a future
    third surface. That is why it is public: reaching for the private
    _lane_entries and re-pairing it with store.pinned() is exactly the seam
    this closes.
    """
    from .. import store
    entries = _lane_entries(project)
    return store.pinned(project=project, entries=entries), entries


def _lanes(text, project=None):
    """(pinned_entries, ALL ranked jit matches, the cached entry list, the df
    map) off ONE
    store parse — the cached list (store entries + the live capability index)
    feeds both lanes explicitly through the entries= seam. JIT comes back
    UNCAPPED so gather can both cap the lane and ledger the pre-cap candidate
    count; the raw list feeds the cooldown scorer and the coinage known-terms
    check without another parse. Capabilities ride the JIT resolver unmodified
    (they are load_class jit, so pinned() ignores them)."""
    from .. import store
    pinned_entries, entries = pinned_lane(project)
    # ONE df map per turn, not two. resolve_prompt computed its own and the
    # cooldown scorer in gather computed a second, identical one over the same
    # candidate set — 0.209s of the turn's 0.592s of construction CPU spent
    # twice on an answer that changes only when the store does.
    df = lane_df(entries, project)
    return (pinned_entries,
            store.resolve_prompt(text, project=project, cap=len(entries),
                                 entries=entries, df=df),
            entries, df)
