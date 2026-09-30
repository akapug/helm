"""The review door: when a chain of review rows should become a meld, and
what a meld must hand back to the rows.

A meld's output is the BAR, not a patch. Once a review chain shows it will
keep going, the next exchange agrees four things: the closed list of harms
that block (everything else is a note), the closed set of falsifier classes
the next read may use, what happens to each open finding, and the exact tip
both sides will judge. A finding outside the bar comes back to the same meld
room, never as a new round.

This module owns the predicates the stop rung and the dispatch door share,
so the two surfaces cannot disagree about what counts as a converged meld.
It reads meld state files and room rows through their public readers, opens
a task's pair-meld rounds only through `meld.invite`, and never reads or
writes the meld lifecycle journal itself: the one thing it takes from that
directory is its file NAMES, to find the pair rooms that exist.

It also owns the PAIR MELD (one persistent room per task, one round per
dispatch; see that section below), because the door is where every dispatch
passes and the room is where the door's own melds now live.
"""
import hashlib
import os
import re

from . import pk


#: The four things a review meld agrees, in the order it agrees them.
BAR_FIELDS = ("BAR", "FINDINGS", "TIP", "NEXT")
#: The closed set of falsifier classes the next read may bind. Required on
#: an AGREED block and optional on the others: a meld that agrees a shape and
#: leaves the falsifier set open has not converged (store move
#: a-meld-converges-the-falsifier-set-not-only-the-shape).
FALSIFIERS_FIELD = "FALSIFIERS"
OUTCOME_FIELDS = BAR_FIELDS + (FALSIFIERS_FIELD,)
#: How a meld ended. Only AGREED converges anything.
OUTCOME_WORDS = ("AGREED", "SPLIT", "RESEARCH")
FINDING_DISPOSITIONS = frozenset(("cured-in-patch", "inside-bar", "note",
                                  "refuted"))
OUTCOME_HEAD = "MELD OUTCOME"
#: What `parse_outcome` says of a closing line that carries no block at all.
NO_BLOCK = "no %s block" % OUTCOME_HEAD
#: A falsifier value that names no falsifier.
_NO_FALSIFIER = frozenset(("none", "n/a", "na", "-", "nil", "tbd", "?"))
#: A FINDINGS value that names no open finding: the plan round and a clean
#: focused check have none, and FINDINGS is still a required field. Unlike
#: the falsifier placeholders, `tbd` and `?` are not here: an undecided
#: disposition is not an empty set.
_NO_FINDING = frozenset(("none", "n/a", "na", "-", "nil", "zero"))

#: THE FIVE-FIELD BLOCK, spelled once. A refused block prints it as its
#: fix, and the bar brief, docs/NEW_AGENT_GUIDE.md and `helm chat meld`'s
#: usage show it, so the line a seat copies is the line the parser reads.
OUTCOME_LINE = ("MELD OUTCOME: AGREED|SPLIT|RESEARCH | BAR: <harms> | "
                "FALSIFIERS: <class>; ... | FINDINGS: <id>=<disposition>; ... "
                "| TIP: <full sha> | NEXT: <next action>")

#: The brief a review meld opens with, and the brief a row carries when the
#: meld could not be opened. One line per item so it survives any renderer.
BAR_TEMPLATE = (
    "MELD BAR — agree these four before anyone researches a finding:\n"
    "  1. BLOCKING HARMS: the closed list of harms that block this lane; "
    "everything else is a note, filed as remainder.\n"
    "  2. FALSIFIERS: the closed set of falsifier classes the next read may "
    "use; a new class comes back to this room, never as a new round.\n"
    "  3. FINDINGS: each open finding's disposition "
    "(cured-in-patch | inside-bar | note | refuted), or `none` when no "
    "finding is open.\n"
    "  4. TIP: the exact tip both sides will judge.\n"
    "Each side's last [DONE] carries one line:\n"
    "  " + OUTCOME_LINE + "\n"
    "An AGREED block with no FALSIFIERS has not converged: the bar is still "
    "open.\n"
    "After agreement, a brief that names REVIEW FIX MODE follows it: "
    "PATCH commits the cure and returns FIX with the reviewer's patch tip; "
    "MELD-DIFF posts the exact diff in the pair meld for the author to apply, "
    "then re-reads that cure before a source-clean hold. Otherwise the "
    "reviewer fixes findings in this pass. Cite the meld on the row's answer.")


def bar_topic(lane):
    """The meld's problem statement for a lane whose next exchange is the
    bar. Kept short: it is the seed row's head and the room name's slug."""
    return ("%s: agree the bar (blocking harms, falsifier classes, each "
            "finding's disposition, the tip)" % lane)


_CHAIN_MARK = re.compile(r"\(chain ([0-9a-f]{12})\)")


def _chain_id(chain):
    """The 12-character chain id a marker carries, or "" for a chain key
    that is not an id (a legacy lane-keyed chain, or none)."""
    chain12 = str(chain or "")[:12]
    return chain12 if re.fullmatch(r"[0-9a-f]{12}", chain12) else ""


def chain_mark(chain):
    """` (chain <id12>)` for a chain id, or "" for a chain that has none.
    Every invite printed for a chain carries it: the marker is what binds a
    meld to this chain and no other (`about_chain`)."""
    chain12 = _chain_id(chain)
    return " (chain %s)" % chain12 if chain12 else ""


def meld_invite(peer, lane, prescription="MELD", chain=None):
    """The literal invite command a surface prints, for the reading given.

    MELD converges the open findings of a chain that argues about them; an
    UNDER-ARMED chain has no open finding to converge and needs the BAR
    instead, because each read keeps finding what the last one's arms could
    not see. The topic names the chain id when there is one, which is what
    binds the meld to this chain and no other (`about_chain`)."""
    topic = (bar_topic(lane) if prescription == "UNDER-ARMED" else
             "%s: converge every open review finding in ONE exchange" % lane)
    topic += chain_mark(chain)
    return 'helm chat meld invite %s "%s"' % (peer, topic)


_FULL_SHA = re.compile(r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_ROOM = re.compile(r"meld-\d+-[a-z0-9-]{0,64}\Z")
_DONE_TAIL = re.compile(r"\s*\[DONE\]\s*$")


def is_meld_room(room):
    """Is `room` shaped like a room `meld invite` names (meld-<epoch>-<slug>)?"""
    return bool(_ROOM.fullmatch(str(room or "")))


def parse_outcome(text):
    """(outcome dict, None) or (None, why) for one closing chunk's text.

    The block is `MELD OUTCOME: <word> | BAR: .. | FALSIFIERS: .. | FINDINGS:
    .. | TIP: <sha> | NEXT: ..`, its fields split on `|` or newlines, in any
    order after the word. The four bar fields are required and TIP must be
    one full commit id: a block that names no tip names no tree anybody can
    judge. AGREED also requires a FALSIFIERS set naming at least one class:
    an agreement that leaves the falsifier set open has not converged, and
    the next read would measure a new true spelling (falsifier (d) of the
    pair meld).

    AN AMBIGUOUS BLOCK IS REFUSED, never read by its last word: a second
    block in one chunk, a field named twice, or a finding id given two
    dispositions (case and spacing aside) says two things, and a reader
    that keeps one of them can read an agreement nobody made."""
    from . import chat
    text = str(text or "")
    head = OUTCOME_HEAD + ":"
    at = text.upper().find(head)
    if at < 0:
        return None, NO_BLOCK
    if text.upper().count(head) > 1:
        return None, "two %s blocks in one chunk" % OUTCOME_HEAD
    body = _DONE_TAIL.sub("", text[at + len(OUTCOME_HEAD) + 1:])
    parts = [p.strip() for p in re.split(r"[|\n]", body) if p.strip()]
    word = parts[0].split()[0].upper() if parts and parts[0].split() else ""
    if word not in OUTCOME_WORDS:
        return None, "outcome word is not one of %s" % "|".join(OUTCOME_WORDS)
    out, seen = {"outcome": word}, set()
    for part in parts[1:]:
        key, sep, value = part.partition(":")
        key = key.strip().upper()
        if not sep or key not in OUTCOME_FIELDS:
            continue
        if key in seen:
            return None, "%s block names %s twice" % (OUTCOME_HEAD, key)
        seen.add(key)
        if value.strip():
            out[key.lower()] = value.strip()
    missing = [k for k in BAR_FIELDS if k.lower() not in out]
    if missing:
        # EVERY FIELD IT LACKS, in the line's order: an AGREED block owes its
        # falsifier set too, and a seat told four fields types four and is
        # refused again for the fifth.
        if not falsifier_set(out.get("falsifiers")) and word == "AGREED":
            missing.insert(int(missing[0] == "BAR"), FALSIFIERS_FIELD)
        return None, _outcome_fix("%s block lacks %s" % (
            OUTCOME_HEAD, ", ".join(missing)))
    bad = _finding_error(out["findings"])
    if bad:
        return None, _outcome_fix("FINDINGS %s" % bad)
    _pairs, dup = _finding_pairs(out["findings"])
    if dup:
        # Another seat's bytes, laundered where they enter text.
        return None, "FINDINGS names finding %s twice" % chat._dsan(dup[:40])
    tip = out["tip"].split()[0].lower()
    if not _FULL_SHA.fullmatch(tip):
        return None, "TIP is not one full commit id"
    out["tip"] = tip
    if word == "AGREED" and not falsifier_set(out.get("falsifiers")):
        return None, _outcome_fix(
            "AGREED names no FALSIFIERS: a meld that agrees the shape and "
            "leaves the falsifier set open has not converged; name the closed "
            "set of classes the next read may bind")
    return out, None


def _outcome_fix(why):
    """A refusal that names what the block lacks AND the one line that
    fixes it: a seat told only what is missing retypes the block from
    memory and misses the next field."""
    return "%s — the block is one line: %s" % (why, OUTCOME_LINE)


def falsifier_set(value):
    """The falsifier classes a FALSIFIERS field names, as a frozenset of
    normalized items split on `;`. Case and spacing are not content, and a
    placeholder (`none`, `n/a`, `-`) names nothing."""
    items = {" ".join(x.split()).casefold()
             for x in str(value or "").split(";")}
    return frozenset(x for x in items if x and x not in _NO_FALSIFIER)


def room_outcome(room, rows=None, epoch=None):
    """What a meld room's closing chunks say, read off its rows.

    -> {"room", "epoch", "seed", "parties", "spoke", "outcomes": {seat:
        dict|None}, "whys": {seat: why}, "agreed": bool, "outcome": word|None,
        "tip": sha|None, "bytes": int, "why": str}

    THE ROUND READ IS THE NEWEST ONE, or `epoch` when named. A persistent
    room (a task's pair meld) holds one round per dispatch; reading its FIRST
    seed judged the plan round forever and every later agreement was
    invisible. `bytes` is the whole room's text, every round: what the
    conversation cost so far.

    PARTIES are the seed's convener and its pinned invited set. Each party's
    LAST [DONE] chunk in the seed's epoch is the one read: a party that said
    DONE twice changed its mind, and the later word stands.

    AGREED IS ONE AGREEMENT, NOT TWO THAT USE THE SAME WORD. It needs every
    party to have spoken and every party's block to read AGREED on the SAME
    bar, the same disposition for every finding, the same tip and the same
    next action. Two AGREED blocks that differ on any of the four are a
    SPLIT, and so is one without a tip in common: `outcome` is AGREED exactly
    when `agreed` is true, so no reader can record the word without the
    agreement. An unreadable room is not an empty one; it answers `why` and
    agrees nothing."""
    from . import chat, meld
    out = {"room": room, "epoch": None, "seed": "", "parties": [],
           "spoke": [], "outcomes": {}, "whys": {}, "agreed": False,
           "outcome": None, "tip": None, "bytes": 0, "why": ""}
    if not is_meld_room(room):
        out["why"] = "not a meld room name"
        return out
    if rows is None:
        try:
            rows, _total, fault = chat.read_checked(room, 0)
        except Exception as e:                          # noqa: BLE001
            rows, fault = [], "%s: %s" % (type(e).__name__, e)
        if fault:
            out["why"] = "room unreadable (%s)" % fault
            return out
    out["bytes"] = sum(len((m.get("text") or "").encode("utf-8"))
                       for m in rows or ())
    if epoch is None:
        epoch, convener, seedtext = meld.latest_seed(rows)
    else:
        convener = seedtext = None
        for ep, conv, text, _i in meld.seeds(rows):
            if ep == epoch:
                convener, seedtext = conv, text
                break
    if epoch is None:
        out["why"] = "no meld seed in the room"
        return out
    if seedtext is None:
        out["why"] = "no round e:%d in the room" % epoch
        return out
    out["epoch"] = epoch
    parties = [convener] + [p for p in meld._invited_seats(seedtext)
                            if p != convener]
    out["parties"], out["seed"] = parties, seedtext
    spoke, last_done = set(), {}
    for m in rows:
        text, frm = m.get("text") or "", str(m.get("from") or "")
        if frm not in parties or m.get("react"):
            continue
        em = meld._EPOCH_RE.search(text)
        if not em or int(em.group(1)) != epoch:
            continue
        mk = meld._MARKER_RE.search(text)
        if not mk:
            continue
        spoke.add(frm)
        if mk.group(1) == "DONE":
            last_done[frm] = text
    out["spoke"] = sorted(spoke)
    for p in parties:
        # A party name is another seat's bytes: laundered where it enters text.
        parsed, why = parse_outcome(last_done.get(p)) if p in last_done \
            else (None, "no [DONE] from %s" % chat._dsan(p))
        out["outcomes"][p] = parsed
        if why:
            out["whys"][p] = why
    parsed = [o for o in out["outcomes"].values() if o]
    words = {o["outcome"] for o in parsed}
    tips = {o["tip"] for o in parsed}
    differ = [field.upper() for field in ("bar", "findings", "next")
              if len({_normal(field, o[field]) for o in parsed}) > 1]
    # THE FALSIFIER SET IS PART OF THE AGREEMENT: two AGREED blocks that
    # bind different classes agreed two different bars.
    if len({falsifier_set(o.get("falsifiers")) for o in parsed
            if o["outcome"] == "AGREED"}) > 1:
        differ.append(FALSIFIERS_FIELD)
    if len(tips) > 1:
        differ.append("TIP")
    if len(parsed) == len(parties) and len(parties) >= 2:
        out["outcome"] = words.pop() if len(words) == 1 else "SPLIT"
        out["tip"] = tips.pop() if len(tips) == 1 else None
    out["agreed"] = (out["outcome"] == "AGREED" and out["tip"] is not None
                     and not differ and set(parties) <= spoke)
    if out["outcome"] == "AGREED" and not out["agreed"]:
        out["outcome"] = "SPLIT"      # the word without the agreement
    if not out["agreed"]:
        out["why"] = "; ".join(
            ["%s: %s" % (chat._dsan(p), w)
             for p, w in sorted(out["whys"].items())]
            + (["the parties' blocks differ on %s" % ", ".join(differ)]
               if differ else [])
            + (["outcome %s" % out["outcome"]]
               if out["outcome"] not in (None, "AGREED") else [])) \
            or "not every party spoke"
    return out


def _finding_error(value):
    """The first malformed `id=disposition` item, or None. A placeholder
    (`_NO_FINDING`) is the empty set, not an item: a round with no open
    finding still owes the field."""
    for item in str(value or "").split(";"):
        key, sep, disposition = item.partition("=")
        key = " ".join(key.split())
        disposition = " ".join(disposition.split()).casefold()
        if not sep and (not key or key.casefold() in _NO_FINDING):
            continue                      # a blank item is not a finding
        if not key:
            return "contains an empty finding id"
        if not sep:
            return ("item %r lacks '=' (a round with no open finding writes "
                    "FINDINGS: none)" % key[:40])
        if disposition not in FINDING_DISPOSITIONS:
            return ("item %r has disposition %r, not one of %s" % (
                key[:40], disposition[:40],
                "|".join(sorted(FINDING_DISPOSITIONS))))
    return None


def _finding_pairs(value):
    """({id: disposition}, the first id named twice or None) for a FINDINGS
    field. Case and spacing are not content, so `F1` and ` f1 ` are one id."""
    pairs, dup = {}, None
    for item in str(value or "").split(";"):
        key, _sep, disp = item.partition("=")
        key = " ".join(key.split()).casefold()
        if not key or (not _sep and key in _NO_FINDING):
            continue                      # `none` and `-` are one empty set
        if key in pairs and dup is None:
            dup = key
        pairs[key] = " ".join(disp.split()).casefold()
    return pairs, dup


def _normal(field, value):
    """One comparable spelling of a block field: case and spacing are not
    content, and FINDINGS is a set of `id=disposition` pairs in any order
    (`parse_outcome` has already refused an id named twice)."""
    if field == "findings":
        return tuple(sorted(_finding_pairs(value)[0].items()))
    return " ".join(str(value or "").split()).casefold()


def about_chain(got, chain=None, lane=None, tips=()):
    """Was the meld `got` (a `room_outcome`) ABOUT this chain?

    A meld exempts only the chain it was held for: a converged meld with the
    same reader about other work is not this chain's cure. BOTH proofs are
    required; neither is enough alone.

    THE TIP. Every tip the parties' blocks name (the agreed one, or each
    party's in a split) must be one of this chain's own dispatch tips or
    reviewer patches (`tips`). A block about a tree this chain never sent is
    about other work, whatever its problem statement says.

    THE SUBJECT. A problem statement that carries a chain marker is about
    exactly the chain it names: every marker must be this chain's id, and a
    marker never falls through to the lane. A statement with no marker binds
    by naming the lane, and only for a caller that has no chain id to match
    — a first build row's design meld (T0), sent before its chain exists,
    or a legacy lane-keyed chain. Every invite printed for a chain with an
    id carries its marker (`chain_mark`), so a caller with an id requires
    it: a lane label is reused across unrelated chains. The marker is the
    literal token `(chain <id12>)`; the id spelled any other way (bare,
    `chain/<id>`, `opening-chain=<id>`, the full id) is prose and binds
    nothing. `off_chain` says which proof failed."""
    return not off_chain(got, chain, lane, tips)[0]


def off_chain(got, chain=None, lane=None, tips=()):
    """(proof, why): which of `about_chain`'s two proofs the meld `got`
    fails, `tip` or `subject`, and why; ("", "") when it was about the
    chain. One reader, so a refusal never restates the rule in words the
    check does not accept: a subject answer spells the marker token."""
    got = got or {}
    wanted = {str(t).lower() for t in tips or () if t}
    named = ({got["tip"]} if got.get("tip") else
             {o["tip"] for o in (got.get("outcomes") or {}).values() if o})
    if not named:
        return "tip", "its blocks name no tip"
    if not named <= wanted:
        return "tip", ("a tip its blocks name (%s) is not one this record is "
                       "about (%s)" % (
                           ", ".join(sorted(t[:12] for t in named - wanted)),
                           ", ".join(sorted(t[:12] for t in wanted)) or "none"))
    seed = str(got.get("seed") or "")
    marks = set(_CHAIN_MARK.findall(seed))
    chain12 = _chain_id(chain)
    if marks == {chain12}:
        return "", ""
    others = ", ".join("(chain %s)" % m for m in sorted(marks - {chain12}))
    if marks and chain12:
        return "subject", ("its problem statement carries another chain's "
                           "marker, %s, and binds only when every marker is "
                           "this row's (chain %s)" % (others, chain12))
    if marks:
        return "subject", ("its problem statement carries a chain marker, "
                           "%s, and this row has no chain id to match it"
                           % others)
    if chain12:
        return "subject", ("its problem statement carries no chain marker: "
                           "the marker is the literal token (chain %s), and "
                           "the id spelled any other way binds nothing"
                           % chain12)
    if lane and str(lane) in seed:
        return "", ""
    return "subject", ("its problem statement carries no chain marker and "
                       "does not name lane %r" % (lane,))


def converged(st, peer, chain=None, lane=None, tips=()):
    """Did the meld behind state `st` CONVERGE with `peer` about THIS chain,
    so it may switch the spiral rung off? A closed meld the peer spoke in
    (the state's own proof, `meld.converged_with`), every party's last
    [DONE] carrying the same AGREED block (the room's proof), and the meld
    about the chain (`about_chain`). A meld the reviewer left to research,
    one side closed alone, a SPLIT, or a meld about other work buys
    nothing."""
    from . import meld
    if not meld.converged_with(st, peer):
        return False
    room = str((st or {}).get("room") or "")
    got = room_outcome(room)
    # EXACT-ROUND AUTHORITY (falsifier (h)). A pair meld's room holds every
    # round of its task, and a state vouches only while its round is the
    # room's newest: once the next round opens, round N's agreement is
    # history, and it exempts round N+1 of nothing.
    if is_pair_room(room) and st.get("epoch") != got["epoch"]:
        return False
    return got["agreed"] and about_chain(got, chain, lane, tips)


_MELD_REF = re.compile(r"(meld-\d+-[a-z0-9-]{0,64})(?:@(\d{1,12}))?\Z")
_SEED_ROW = re.compile(r"\brow ([0-9a-f]{12}) at ")
_SEED_CHAIN = re.compile(r"\(chain ([0-9a-f]{12})\)")
_OPENING = re.compile(
    r"\A(?:\s*\[MELD e:\d+\] PROBLEM:\s+)?"
    r"opening-row=([0-9a-f]{12})\s+"
    r"opening-chain=([0-9a-f]{12})(?:\s|\||$)")
_LEGACY_PAIR_BOUNDARY = re.compile(r" \| round \d+ \| pairing: ")


def split_meld_ref(ref):
    """(room, epoch | None) for `ROOM` or `ROOM@EPOCH`; (None, None) for a
    reference that is neither."""
    m = _MELD_REF.fullmatch(str(ref or "").strip())
    if not m:
        return None, None
    return m.group(1), (int(m.group(2)) if m.group(2) else None)


def _legacy_pair_head(seedtext):
    """Generated legacy topic head, or None when its delimiter is ambiguous."""
    body = str(seedtext or "").split("PROBLEM:", 1)[-1]
    boundaries = list(_LEGACY_PAIR_BOUNDARY.finditer(body))
    if len(boundaries) != 1:
        return None
    return body[:boundaries[0].start()]


def _seed_row(seedtext):
    """The dispatch row id12 a pair round's seed names, or None."""
    text = str(seedtext or "")
    marked = _OPENING.search(text)
    if marked:
        return marked.group(1)
    head = _legacy_pair_head(text)
    found = _SEED_ROW.findall(head or "")
    return found[-1] if found else None


def _seed_chain(seedtext):
    """The exact chain id12 structurally bound by one pair seed, or None."""
    text = str(seedtext or "")
    marked = _OPENING.search(text)
    if marked:
        return marked.group(2)
    head = _legacy_pair_head(text)
    found = _SEED_CHAIN.findall(head or "")
    return found[-1] if found else None


def meld_citation(room, row, tips):
    """(fields, None) or (None, why): what a verdict or hold that cites
    `--meld ROOM` or `--meld ROOM@EPOCH` records on its row.

    `ROOM` reads the room's newest round; `ROOM@EPOCH` reads the named one.
    EXACT-ROUND AUTHORITY (falsifier (h)): a pair meld's round was opened by
    one dispatch row, and its MELD OUTCOME closes that row only. Round N's
    agreement cited on round N+1's row is refused by name, whatever its
    parties and tip say, and the refusal names the epoch to cite instead.
    The recorded `meld_epoch` is the round actually read.

    The meld must be BETWEEN the row's author and its reader, both must have
    closed with a MELD OUTCOME block, the tip the block names must be one
    this record is about (`tips`), and the meld must have been about this
    row's work (`about_chain`). The recorded word is lower-case, and only
    `agreed` switches the spiral rung off; a SPLIT or RESEARCH meld is still
    recordable, because the verdict that follows it is real."""
    from . import chat
    ref = str(room or "")
    room, epoch = split_meld_ref(ref)
    got = room_outcome(room or ref, epoch=epoch)
    if not got["parties"]:
        return None, "--meld %s: %s" % (ref, got["why"] or "no meld here")
    if is_pair_room(room):
        named = _seed_row(got["seed"])
        mine = str(row.get("id") or "")[:12]
        # T0's design meld exists before its first irreversible build row and
        # therefore has no row id on either side. Every post-write citation has
        # a row id and must find the same id in the seed.
        if mine and not named:
            return None, (
                "--meld %s: round e:%d does not durably name its opening "
                "dispatch row — exact-round authority cannot bind the outcome"
                % (ref, got["epoch"]))
        if named and not mine:
            return None, (
                "--meld %s: this row has no id to match round e:%d's opening "
                "dispatch %s" % (ref, got["epoch"], named))
        if named and named != mine:
            return None, (
                "--meld %s: round e:%d of this pair meld was opened for row "
                "%s, not this one (%s) — exact-round authority: a round's "
                "MELD OUTCOME closes only its own row, so cite this row's "
                "round as %s@<epoch> (helm chat read --room %s)"
                % (ref, got["epoch"], named, mine, room, room))
    wanted = {str(t or "").lower() for t in tips if t}
    who = {str(row.get("sender") or ""), str(row.get("recipient") or "")}
    if not who <= set(got["parties"]):
        return None, ("--meld %s was between %s, not this row's author and "
                      "reader (%s)" % (room, ", ".join(
                          chat._dsan(p) for p in got["parties"]),
                                       ", ".join(sorted(who))))
    if got["outcome"] is None:
        return None, ("--meld %s has no readable MELD OUTCOME from every "
                      "party: %s" % (room, got["why"]))
    why = _tip_refusal(room, got["tip"], wanted)
    if why:
        return None, why
    why, fix = _subject_refusal(room, got, row, wanted)
    if why:
        return None, why + ("; a meld that binds opens with %s"
                            % chat._dsan(fix) if fix else "")
    # EXACTLY THE ROOM'S VERDICT: `outcome` reads AGREED only when the
    # parties agreed, so the recorded word can never outrun the agreement.
    # `meld_bytes` is the room's size at the moment the outcome reaches the
    # row: the pair meld's cost, recorded once where the ledger keeps it,
    # so a chain closed through a meld can be weighed against one closed on
    # rows alone (falsifier (g)) after the room itself is retired.
    return {"meld_room": room,
            "meld_outcome": str(got["outcome"]).lower(),
            "meld_bytes": int(got["bytes"]),
            "meld_epoch": int(got["epoch"])}, None


def _tip_refusal(room, tip, wanted):
    """The citation's refusal of an agreed `tip` that is not one of the tips
    this record is about (`wanted`), or None. A round with no agreed tip (a
    split) is refused elsewhere, never here."""
    if tip is None or tip in wanted:
        return None
    return ("--meld %s agreed on tip %s, which this record is not about (%s)"
            % (room, tip[:12], ", ".join(sorted(t[:12] for t in wanted))))


def _subject_refusal(room, got, row, wanted, peer=None):
    """(why, fix): the citation's refusal when meld `got` was not about
    `row`'s work, and the invite that opens a meld that binds; ("", "")
    when it was about it.

    THE REFUSAL NAMES THE PROOF THAT FAILED, and a subject refusal on a row
    with a chain id carries the invite whose topic carries the marker: told
    only to "name chain <id12>", a seat names it in prose, and prose binds
    nothing. The invite goes to `peer`, the row's author by default: the
    reader is the one who cites."""
    proof, why = off_chain(got, row.get("chain_root"), row.get("lane"),
                           wanted)
    if not proof:
        return "", ""
    fix = ""
    if proof == "subject" and _chain_id(row.get("chain_root")):
        fix = meld_invite(peer or row.get("sender"), row.get("lane"),
                          "UNDER-ARMED", row.get("chain_root"))
    return "--meld %s was not about this work: %s" % (room, why), fix


# ---------------------------------------------------------------------------
# THE CLOSING CHECK: the door's refusals, said before a round seals
# ---------------------------------------------------------------------------
# A round seals on its last [DONE], and a sealed room takes nothing further.
# A party that closed with a block the door cannot read, or in a round whose
# problem statement does not bind the row, learned it only when `hold
# --meld` or `verdict --meld` refused, and every row then needed a fresh
# round. `meld say --marker DONE` asks the door's own predicates first and
# refuses before anything posts. Nothing here is a second grammar or a
# second binding rule: THE BLOCK is `parse_outcome`, THE BINDING is
# `_subject_refusal`, the subject proof the citation and the stop rung's
# `converged` both require (`off_chain`).

def door_reads(room, seedtext):
    """Does the review door read the round of `room` whose seed is
    `seedtext`, by what the round IS: a task's pair meld (a room named for
    its chain), or a problem statement that binds a chain (its `(chain
    <id12>)` marker, or a generated opening field)? A meld that is neither
    is a design meld, and closes as it likes."""
    seedtext = str(seedtext or "")
    return bool(is_pair_room(room) or _CHAIN_MARK.search(seedtext)
                or _seed_chain(seedtext))


_PAIR_CHAIN = re.compile(r"-chain-([0-9a-f]{12})\Z")


def _bound_rows(room, seed, parties, current):
    """The ledger rows the door binds this round to, between its parties,
    newest first: the row that opened a pair round (exact-round authority);
    else every row of each chain the round names (a `(chain <id12>)` marker,
    a generated opening chain, the chain a pair room is named for). [] for a
    round that binds no row: a design meld, or T0's round before its row
    exists."""
    def ours(r):
        return {str(r.get("sender") or ""),
                str(r.get("recipient") or "")} <= parties
    opening = _seed_row(seed) if is_pair_room(room) else None
    named = _PAIR_CHAIN.search(room) if is_pair_room(room) else None
    chains = set(_CHAIN_MARK.findall(seed)) | {
        _seed_chain(seed), named.group(1) if named else None}
    rows = [r for r in current.values() if ours(r) and (
        str(r.get("id") or "")[:12] == opening if opening
        else str(r.get("chain_root") or "")[:12] in chains - {None})]
    return sorted(rows, key=lambda r: str(r.get("ts") or ""), reverse=True)


def _bound_tip_refusal(room, tip, row):
    """(why, fix): the citation's refusal of closing tip `tip` on the row the
    round is bound to, and the remedy; ("", "") when the row's record can
    be about `tip`. A citation takes the row's dispatched tip, a reviewer's
    patch that descends from it, or a source-clean tip that descends from
    it, and both descents are proven by the hold door's lineage rule."""
    from . import dispatches
    dispatched = str(row.get("tip") or "").lower()
    why = _tip_refusal(room, tip, {dispatched})
    if not why:
        return "", ""
    lineage = dispatches._source_clean_lineage_error(row.get("repo_root"),
                                                     row, tip)
    if lineage is None:
        return "", ""
    return ("%s; as a patch or source-clean tip, %s" % (why, lineage),
            "close it on a TIP this row's record is about: its dispatched "
            "tip %s, or a commit that descends from it" % dispatched)


def done_refusal(room, text, epoch=None, seat=None, current=None):
    """The refusal a [DONE] posting `text` (the whole row, as it will post)
    into round `epoch` of meld `room` earns from the review door, said before
    it posts; or None.

    THE BLOCK is read by `parse_outcome`, the one validator the citation
    reads every party's block with (`room_outcome`), so a block that seals
    is a block the citation can read. Any closing line that carries a MELD
    OUTCOME block is checked, in every room: the door reads a block
    wherever it is cited, and a T0 design meld's room carries no pair name
    and no chain marker. A round the door reads (`door_reads`) also owes
    the block; a design meld closes with no block, as before.

    THE BOUND ROW'S TIP. A round the door binds to rows (`_bound_rows`)
    closes only on a tip one of those rows' records is about
    (`_bound_tip_refusal`, the citation's own tip rule), whatever tip the
    block names.

    THE BINDING. A block that names the tip of a dispatch row between this
    round's parties CLAIMS that row, in any room. The door counts the round
    for the row only when its problem statement names the row's chain, so
    when that proof fails for every such row, this refuses too, with the
    invite that opens a round that binds.

    A round that binds no row and names no dispatched tip (T0's design meld,
    sent before its row exists) is left to the door, and so is a ledger that
    cannot be read: this is the door's word said early, never a second
    door."""
    from . import chat, meld
    rows = chat.read(room)[0]
    found = [(conv, seed) for ep, conv, seed, _i in meld.seeds(rows)
             if ep == epoch]
    if found:
        convener, seed = found[-1]
    else:
        _epoch, convener, seed = meld.latest_seed(rows)
    seed = seed or ""
    parsed, why = parse_outcome(text)
    if why:
        reads = door_reads(room, seed)
        if why == NO_BLOCK and not reads:
            return None
        what = ("a task's pair meld" if is_pair_room(room) else
                "its problem statement binds a chain" if reads else
                "the door reads a block wherever it is cited")
        return ("MELD-OUTCOME-REFUSED room=%s — the review door reads this "
                "block (%s) and would refuse this [DONE]: %s\n  nothing "
                "posted, and the round is not sealed; close it with the block "
                "on one line:\n  %s"
                % (room, what, why.replace(_outcome_fix(""), ""),
                   OUTCOME_LINE))
    parties = {str(convener or "")} | set(meld._invited_seats(seed))
    tip = parsed["tip"]
    if current is None:
        from . import dispatches
        try:
            current = dispatches.snapshot()[0] or {}
        except Exception:                               # noqa: BLE001
            return None
    bound = _bound_rows(room, seed, parties, current)
    claimed = bound or sorted(
        (r for r in current.values()
         if str(r.get("tip") or "").lower() == tip
         and {str(r.get("sender") or ""),
              str(r.get("recipient") or "")} <= parties),
        key=lambda r: str(r.get("ts") or ""), reverse=True)
    got, first = {"seed": seed, "tip": tip}, None
    for r in claimed:
        code, (why, fix) = "MELD-TIP-REFUSED", (
            _bound_tip_refusal(room, tip, r) if bound else ("", ""))
        if not why:
            peer = r.get("recipient") if seat == r.get("sender") \
                else r.get("sender")
            code, (why, fix) = "MELD-UNBOUND", _subject_refusal(
                room, got, r, {tip}, peer=peer)
            fix = "close it with --marker ABORT%s" % (
                "; a round that binds opens with:\n  %s" % chat._dsan(fix)
                if fix else "")
        if not why:
            return None
        first = first or (code, r, why, fix)
    if first is None:
        return None
    code, r, why, fix = first
    return ("%s room=%s — the review door would refuse this round on row %s: "
            "%s\n  nothing posted, and the round is not sealed; %s"
            % (code, room, str(r.get("id") or "")[:12], why, fix))


# ---------------------------------------------------------------------------
# THE DOOR: T0-T3 at `dispatch send` / `dispatch add`
# ---------------------------------------------------------------------------
# The stop rung is the only lever that could refuse, and it is easy to pass
# and never fires for a seat whose Stop hook does not run. The send door sees
# every row, so the triggers live here and read the round count from the
# rung's own fold (`dispatches.chain_rounds`): one reader, two surfaces.
#
#   T0  an irreversible target: a design meld before the first build row.
#   T1  round 2, when the last read carries a DESIGN finding (named with
#       --design-finding, or a FIX with --no-patch-because) or the author
#       DISPUTES a finding (--disputes): the door prints the meld for those
#       findings only. A mechanical patch is never held back.
#   T2  round 3 or later: the door OPENS the meld itself when the reader has a
#       live beacon and no open row in flight; otherwise the BAR rides the row
#       as its brief. The row is always created. --async-because REASON skips
#       it and is recorded. It never stops work.
#   T3  an UNDER-ARMED reading makes that meld a bar meld (same room, the bar
#       topic); the stop rung blocks on it instead of advising.
#
# EXEMPT: the reviewer's own adopted patch tip, one tip sent to several
# readers, dispatches nobody answered (all three add no round in the fold),
# a chain whose bar a meld already AGREED, and a reader who does not join
# within the entry window (the row carries the bar; rows reach every seat).

#: How long a door-opened meld waits for its reader before the row, which
#: carries the bar, is the conversation. Peer reply gaps measured at a median
#: of 31s and a 90th percentile of 143s; a cross-family wake can outlast the
#: 90s recv bound several times, and a seat that answers rows but not melds
#: was measured not entering in 24 minutes. Ten minutes covers the first and
#: stops waiting well before the second. HELM_MELD_ENTRY_WINDOW_S moves it.
ENTRY_WINDOW_S = 600


def entry_window_s():
    import os
    try:
        return max(0, int(os.environ.get("HELM_MELD_ENTRY_WINDOW_S")
                          or ENTRY_WINDOW_S))
    except ValueError:
        return ENTRY_WINDOW_S


#: Phrases that name an IRREVERSIBLE target in a build brief (T0). Closed and
#: specific: a word like "delete" alone is ordinary code talk and would wall
#: every refactor, so only a live-system phrase counts. The escape is one
#: recorded flag, so a false hit costs one token.
IRREVERSIBLE_PHRASES = (
    "prod write", "production write", "write to prod", "writes to prod",
    "write to production", "prod data", "production data", "prod database",
    "production database", "prod db", "drop table", "drop column",
    "drop database", "schema migration", "data migration",
    "run the migration", "delete from", "hard delete", "purge the",
    "rotate credentials", "rotate the credential", "credential rotation",
    "revoke the key", "force push", "force-push", "rewrite history",
    "history rewrite", "filter-repo")


def irreversible_hits(text, phrases=IRREVERSIBLE_PHRASES):
    """The declared irreversible-target phrases `text` carries, matched on
    word tokens exactly as the read-only door matches its phrases. The
    safety-door classifier passes its own wider set (`DOOR_PHRASES`)."""
    words = re.findall(r"[a-z0-9]+", str(text or "").lower())
    hits = []
    for phrase in phrases:
        seq = re.findall(r"[a-z0-9]+", phrase)
        n = len(seq)
        if any(words[i:i + n] == seq for i in range(len(words) - n + 1)) \
                and phrase not in hits:
            hits.append(phrase)
    return hits


# ---------------------------------------------------------------------------
# THE SAFETY-DOOR CLASSIFIER: which lanes are doors, and what each touched
# ---------------------------------------------------------------------------
# A lane is a DOOR when it touches prod, a migration, a deletion, money,
# credentials, a process kill or a public push, or a door that decides
# safety: a guard, a hook's refusal, the land or review door. The verdict
# door once withheld a fresh-context Opus read from every door lane (the
# integrator's ruling, chat row 1693). The owner's ruling in room row 2104
# put that read in the approval tier: a door read needs ONE approval-tier
# read by a reader that is not the author, and a different family is no
# longer required. So no door class withholds the read now
# (dispatches._fresh_context_read); the classifier still answers which doors
# a lane is, and every path it touched, which the reader's no-write proof
# reads (runrecord.verify), and a lane whose paths cannot be read refuses
# there.
#
# FOUR ARMS, AND EACH ONE CAN ONLY ADD A DOOR:
#   phrase   the T0 phrases above and the wider door phrases below, in the
#            lane name and its brief;
#   path     every path any commit of base..tip touches, read per commit by
#            compose_contract's reader (a door file changed and changed back
#            is still a door lane), against the exact contract owners the
#            compose exception already vetoes plus DOOR_OWNERS below;
#   scope    a changed line inside a Python function, class, module-level
#            name or import that a guard-, refusal- or kill-named scope
#            REACHES (its own body, or any module-level name that body
#            mentions, to a fixed point). This arm tells a guard lane in a
#            mixed module (the argv-guard lives in chat.py) from a reversible
#            lane in it. It reads one file: a lane that changes only a
#            module the guard imports is not followed there (task/3204);
#   content  a changed line, added or removed, outside the tests, carrying a
#            live-system marker (os.kill, git push, DROP TABLE, ...).
#
# UNKNOWN IS A DOOR. No repository, no base, a diff that cannot be read, an
# empty diff, a merge in the range, or a changed Python file that does not
# parse answers the `unknown` class. Where that leaves `paths` empty, the
# verdict door refuses the read, since no write to the lane can be ruled out.

#: Every class the classifier answers, in the order a refusal names them.
DOOR_CLASSES = ("prod", "migration", "deletion", "money", "credentials",
                "process-kill", "public-push", "guard", "unknown")

#: The phrases per class. It holds every T0 phrase and more: T0 walls a BUILD
#: send, so it stays narrow, while this list only names a lane's doors.
DOOR_PHRASE_CLASSES = (
    ("prod", ("prod write", "production write", "write to prod",
              "writes to prod", "write to production", "prod data",
              "production data", "prod database", "production database",
              "prod db", "deploy to prod", "deploy to production",
              "prod deploy", "production deploy")),
    ("migration", ("drop table", "drop column", "drop database",
                   "schema migration", "data migration",
                   "run the migration")),
    ("deletion", ("delete from", "hard delete", "purge the")),
    ("money", ("money", "payment", "payments", "payout", "payouts", "refund",
               "refunds", "billing", "stripe")),
    ("credentials", ("rotate credentials", "rotate the credential",
                     "credential rotation", "revoke the key", "api key",
                     "oauth token")),
    ("process-kill", ("pkill", "killpg", "sigkill", "kill -9",
                      "kill the process", "kills the process",
                      "kill a process", "process kill")),
    ("public-push", ("force push", "force-push", "rewrite history",
                     "history rewrite", "filter-repo", "git push",
                     "public push", "push public", "gh release")),
    ("guard", ("argv-guard", "hook refusal", "hook's refusal", "stop guard",
               "pretooluse", "safety door")))
DOOR_PHRASES = tuple(p for _c, ps in DOOR_PHRASE_CLASSES for p in ps)

#: Exact door owners beyond compose_contract.PROTECTED_OWNERS, which already
#: names the hook and installed-guard owners and every land, review, gate and
#: ledger contract. A path ending in "/" owns its subtree.
DOOR_OWNERS = (
    ("guard", "helm/actsteer.py"),             # the argv-guard's deny rungs
    ("guard", "helm/seats_stop_guard.py"),     # the Stop hook's one decision
    ("guard", "helm/shaguard.py"),             # the prose sha guard
    ("guard", "helm/hookstdin.py"),            # what a hook payload weighs
    ("guard", "bin/helm-hook"),                # every hook's entry point
    ("guard", "helm/review_door.py"),          # this door and the meld door
    ("guard", "helm/review_independence.py"),  # the independence predicate
    ("guard", "helm/runrecord.py"),            # the run record it verifies
    ("credentials", "helm/cred/"),             # credential homes and hooks
    ("credentials", "helm/creds.py"),          # account rollover
    ("credentials", "helm/codexhomes.py"),     # codex credential homes
    ("money", "helm/codexresets.py"),          # spends scarce reset credits
    ("process-kill", "helm/seatrescue.py"))    # ends a runaway child
#: Door paths by shape, for repositories whose owners helm does not list.
DOOR_PATH_SHAPES = (
    ("migration", re.compile(r"(?:^|/)(?:migrations?|migrate)/")),
    ("prod", re.compile(r"(?:^|/)(?:wrangler\.(?:toml|jsonc?)|deploy[^/]*)\Z")))
#: A scope whose casefolded name matches is a root; a changed line inside a
#: root, or inside any module-level name a root reaches, is a door.
DOOR_SCOPES = (
    ("guard", re.compile(r"guard|refus|deny|denies|block|veto|forbid")),
    ("process-kill", re.compile(r"kill|terminat|(?:^|_)reap")))
#: A changed line carrying one of these markers is a door.
DOOR_MARKERS = (
    ("prod", re.compile(r"\bwrangler\s+deploy\b|\bkubectl\s+apply\b"
                        r"|\bterraform\s+apply\b|\bRAILS_ENV=production\b"
                        r"|--env[= ]prod(?:uction)?\b")),
    ("migration", re.compile(r"(?i:\b(?:alter|drop)\s+(?:table|column|"
                             r"database)\b)|\b(?:add|remove|rename)_column\b")),
    ("deletion", re.compile(r"\bshutil\.rmtree\(|\brm\s+-\w*[rR]\w*f"
                            r"|\brm\s+-\w*f\w*[rR]|\bbranch\s+-D\b"
                            r"|(?i:\bdelete\s+from\b|\btruncate\s+table\b)")),
    ("money", re.compile(r"(?i:\bstripe\b|\bpayments?\b|\bpayouts?\b"
                         r"|\brefunds?\b|\bbilling\b)")),
    ("credentials", re.compile(r"\.credentials\.json|\bcredentials\.env\b"
                               r"|\bauth\.json\b|\b[A-Z]+_API_KEY\b"
                               r"|\b(?:refresh|access)_token\b")),
    ("process-kill", re.compile(r"\bos\.kill(?:pg)?\(|\bSIG(?:KILL|TERM)\b"
                                r"|\bpkill\b|\bkillall\b|\bkill\s+-\w"
                                r"|\.(?:terminate|kill)\(\)")),
    ("public-push", re.compile(r"\bgit\s+push\b|\bgh\s+(?:release|repo\s+"
                               r"(?:create|edit))\b|--force-with-lease"
                               r"|\bfilter-repo\b|[\"'](?:git|gh)[\"']\s*,\s*"
                               r"[\"']push[\"']|\brun\([^)\n]*[\"']push[\"']")),
    ("guard", re.compile(r"argv-guard\]|\bBLOCKED:|\bpermissionDecision\b"
                         r"|[\"']decision[\"']\s*:\s*[\"']block"
                         r"|\bsys\.exit\(2\)|(?:^\s*|[;&|]\s*|\bthen\s+)exit\s+2\b")))
#: A test or a document acts on no live system: the scope and content arms
#: skip it (the phrase arm still reads the lane's own words).
_TEST_FILE = re.compile(r"(?:^|/)(?:tests?|__tests__|spec)/|(?:^|/)test_[^/]*"
                        r"\Z|_test\.[^/]+\Z|\.(?:test|spec)\.[^/]+\Z"
                        r"|\.(?:md|rst|txt)\Z")
_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def lane_doors(row, current=None):
    """{"doors": [(class, evidence)], "paths": [path], "base", "tip"} — every
    door this row's lane is, from the four arms above. No door means the lane
    is REVERSIBLE; an `unknown` door means it could not be told. `paths` is
    every path the lane touched, for the reader's own write check
    (runrecord.verify), and empty when the lane's diff cannot be read, which
    the verdict door refuses."""
    from . import dispatches
    tip = str((row or {}).get("tip") or "").lower()
    out = {"doors": [], "paths": [], "base": None, "tip": tip or None}
    body, _absent, _problem = dispatches.brief_of(row or {})
    for phrase in irreversible_hits("%s\n%s" % ((row or {}).get("lane") or "",
                                                body or ""), DOOR_PHRASES):
        out["doors"].append((next(c for c, ps in DOOR_PHRASE_CLASSES
                                  if phrase in ps), "the lane or its brief "
                             "names %r" % phrase))
    repo = (row or {}).get("repo_root") or (row or {}).get("repo_id")
    if not (isinstance(repo, str) and os.path.isabs(repo)
            and os.path.isdir(repo)) or not _FULL_SHA.fullmatch(tip):
        out["doors"].append(("unknown", "the row names no readable repository "
                             "or no exact tip, so its diff cannot be read"))
        return _ordered(out)
    base, why = _lane_base(repo, row, tip, current)
    if why:
        out["doors"].append(("unknown", why))
        return _ordered(out)
    out["base"] = base
    from . import compose_contract
    diff = compose_contract.measure_diff(repo, base, tip)
    if not diff:
        out["doors"].append(("unknown", "the per-commit diff of %s..%s cannot "
                             "be read (a merge or an empty commit in the "
                             "range, or git refused)" % (base[:12], tip[:12])))
        return _ordered(out)
    out["paths"] = sorted({p for r in diff for p in r["paths"]})
    for path in out["paths"]:
        if path in compose_contract.PROTECTED_OWNERS:
            out["doors"].append(("guard", "%s is a contract owner the compose "
                                 "exception vetoes" % path))
        for cls, owner in DOOR_OWNERS:
            if path == owner or (owner.endswith("/")
                                 and path.startswith(owner)):
                out["doors"].append((cls, "%s is a %s door owner"
                                     % (path, cls)))
        for cls, shape in DOOR_PATH_SHAPES:
            if shape.search(path):
                out["doors"].append((cls, "%s is shaped like a %s path"
                                     % (path, cls)))
    records = _net_records(repo, base, tip)
    if not records:
        out["doors"].append(("unknown", "the net diff of %s..%s cannot be "
                             "read, so its changed lines cannot be told"
                             % (base[:12], tip[:12])))
    for record in records or ():
        out["doors"].extend(_changed_line_doors(repo, base, tip, record))
    return _ordered(out)


def lane_checkouts(row, current=None):
    """(paths, error) — where a run's Write or Edit changes THE LANE: the
    repository's main worktree (the shared checkout) and the lane's own
    worktree.

    The lane's worktree is any worktree checked out on a branch that this
    row, or a row on its chain, bound as its ref (`ref_branch`), and helm's
    room for each lane name they carry (`<shared>-wt/<lane>`, as
    work._lanes names it), whether or not it is checked out now. A reader's
    private clone is neither, and neither is a worktree it added on a branch
    of its own: the cure it commits there is the review procedure, not a
    write to the lane (integrator ruling, chat row 1915, finding 4). Git's
    worktree list is the registry, and a list that cannot be read refuses:
    where the lane lives is then unknown."""
    from . import vcs
    from .work import _lanes
    row = row or {}
    repo = row.get("repo_root") or row.get("repo_id")
    if not (isinstance(repo, str) and os.path.isabs(repo)
            and os.path.isdir(repo)):
        return None, ("the row names no readable repository, so where the "
                      "lane lives cannot be told")
    try:
        trees, err = vcs.backend(repo).worktrees(repo)
    except (OSError, ValueError) as exc:
        trees, err = [], str(exc)
    if err or not trees:
        return None, ("the repository's worktrees cannot be listed (%s), so "
                      "where the lane lives cannot be told"
                      % (err or "none listed"))
    chain = str(row.get("chain_root") or row.get("id") or "")
    kin = [row] + [r for r in (current or {}).values() if isinstance(r, dict)
                   and chain and str(r.get("chain_root") or r.get("id"))
                   == chain]
    branches = {r["ref_branch"] for r in kin
                if str(r.get("ref_branch") or "").startswith("refs/heads/")}
    names = {str(r.get("lane") or "").strip() for r in kin} | {
        b[len("refs/heads/lane/"):] for b in branches
        if b.startswith("refs/heads/lane/")}
    shared = trees[0]["path"]
    out = [shared] + [t["path"] for t in trees[1:]
                      if t.get("branch") in branches]
    out += [_lanes.lane_path(shared, name) for name in sorted(names)
            if name and ".." not in name.split("/")]
    return list(dict.fromkeys(out)), None


def _ordered(out):
    """The doors deduplicated and sorted by DOOR_CLASSES, first seen first."""
    seen, doors = set(), []
    for door in sorted(out["doors"], key=lambda d: DOOR_CLASSES.index(d[0])):
        if door not in seen:
            seen.add(door)
            doors.append(door)
    out["doors"] = doors
    return out


def _lane_base(repo, row, tip, current):
    """(base, why) — where the lane leaves trunk: the merge-base of its tip
    with the trunk ref. A tip already on trunk has no range there, so the
    chain's BUILD row base stands in when it is a strict ancestor of the tip;
    otherwise the base is unknown, never guessed."""
    from . import vcs
    backend = vcs.backend(repo)
    try:
        trunk = backend.trunk_ref(repo)
        rc, base, _err = backend.text(repo, "merge-base", trunk, tip)
    except (OSError, ValueError, UnicodeError) as exc:
        return None, "the lane's base cannot be read (%s)" % exc
    base = str(base or "").strip().lower()
    if rc == 0 and _FULL_SHA.fullmatch(base) and base != tip:
        return base, None
    chain = str((row or {}).get("chain_root") or (row or {}).get("id") or "")
    for other in (current or {}).values():
        if not isinstance(other, dict) or other.get("kind") != "build":
            continue
        cand = str(other.get("tip") or "").lower()
        if chain and _FULL_SHA.fullmatch(cand) and cand != tip \
                and str(other.get("chain_root") or other.get("id")) == chain \
                and backend.text(repo, "merge-base", "--is-ancestor", cand,
                                 tip)[0] == 0:
            return cand, None
    return None, ("the lane's base cannot be told: its tip %s has no range "
                  "off %s and no build row on its chain records where it "
                  "was cut" % (tip[:12], trunk))


def _net_records(repo, base, tip):
    """The net name-status records of base..tip, or None when unreadable."""
    from . import compose_contract, vcs
    try:
        rc, raw, _err = vcs.backend(repo).run(
            repo, "diff", "--no-ext-diff", "--no-textconv", "--name-status",
            "-z", "--find-renames", base, tip, "--")
    except (OSError, ValueError):
        return None
    return compose_contract._diff_records(raw) if rc == 0 else None


def _changed_line_doors(repo, base, tip, record):
    """The scope and content arms over one changed file of the net diff."""
    from . import vcs
    old = record["paths"][0] if record["status"] != "A" else None
    new = record["paths"][-1] if record["status"] != "D" else None
    path = new or old
    if _TEST_FILE.search(path):
        return []
    try:
        rc, raw, _err = vcs.backend(repo).run(
            repo, "diff", "--no-ext-diff", "--no-textconv", "--no-color",
            "-U0", "--find-renames", base, tip, "--",
            *[p for p in (old, new) if p])
    except (OSError, ValueError) as exc:
        return [("unknown", "the diff of %s cannot be read (%s)"
                 % (path, exc))]
    if rc != 0:
        return [("unknown", "the diff of %s cannot be read" % path)]
    lines = {"old": set(), "new": set()}
    doors, in_hunk = [], False
    for line in raw.decode("utf-8", "replace").splitlines():
        hunk = _HUNK.match(line)
        if hunk:
            in_hunk = True
            start, count = int(hunk.group(1)), int(hunk.group(2) or 1)
            lines["old"].update(range(start, start + count))
            start, count = int(hunk.group(3)), int(hunk.group(4) or 1)
            lines["new"].update(range(start, start + count))
        elif in_hunk and line[:1] in ("+", "-"):
            doors.extend((cls, "%s changes a line carrying %r"
                          % (path, marker.group(0)))
                         for cls, rx in DOOR_MARKERS
                         for marker in [rx.search(line[1:])] if marker)
    if not path.endswith(".py"):
        return doors
    for side, sha, name in (("old", base, old), ("new", tip, new)):
        if name and lines[side]:
            doors.extend(_scope_doors(repo, sha, name, lines[side]))
    return doors


def _scope_doors(repo, sha, path, changed):
    """The scope arm: a changed line inside a module-level function, class,
    name or import that a guard-, refusal- or kill-named scope REACHES.

    A name is only where the reading starts. The argv-guard's refusals live
    in helpers named for what they parse (a spelled verb, a data predicate),
    and a lane that changes one of them changes what the guard refuses: over
    the real history of chat.py, a name-only arm read 21 of 36 argv-guard
    commits as reversible. So each root (a scope whose own name, or a nested
    def's name, matches DOOR_SCOPES) is followed through every module-level
    name its body mentions, to a fixed point, and a changed line inside any
    scope it reaches is the root's door.

    AN IMPORT IS A SCOPE TOO. A lane that rebinds what a guard calls on an
    import line (another helper under the same name, another module under
    the same alias, a new import that shadows it) changed no function, class
    or assignment, so it read REVERSIBLE (the Fable read of this lane,
    integrator ruling at chat row 1915). Each name an import binds is a
    scope whose node is the import statement, so a guard that mentions the
    name reaches the statement. WHAT THIS DOES NOT FOLLOW is the imported
    module itself: a lane that changes only a module the guard calls into
    (registry, scratch, seats_* under the argv-guard) still reads reversible
    here, and reaching across files is task/3204."""
    import ast
    from . import vcs
    try:
        rc, raw, _err = vcs.backend(repo).run(repo, "show",
                                              "%s:%s" % (sha, path))
        tree = ast.parse(raw.decode("utf-8")) if rc == 0 else None
    except (OSError, ValueError, SyntaxError, UnicodeError):
        tree = None
    if tree is None:
        return [("unknown", "%s at %s cannot be parsed, so what its changed "
                 "lines decide cannot be told" % (path, sha[:12]))]
    scopes = {}               # module-level name -> the statements binding it
    for name, node in _bindings(tree.body):
        scopes.setdefault(name, []).append(node)
    # A star import binds names nobody can list, so any name a scope
    # mentions may be one it binds: every scope reaches it.
    uses = {name: ({n.id for node in nodes for n in ast.walk(node)
                    if isinstance(n, ast.Name)} | {"*"}) & set(scopes)
            for name, nodes in scopes.items()}
    doors = []
    for cls, rx in DOOR_SCOPES:
        roots = {name for name, nodes in scopes.items()
                 if any(rx.search(str(getattr(n, "name", name)).casefold())
                        for node in nodes for n in ast.walk(node)
                        if n is node or isinstance(
                            n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                ast.ClassDef)))}
        via = {name: name for name in roots}
        frontier = sorted(roots)
        while frontier:
            name = frontier.pop()
            for used in sorted(uses[name] - set(via)):
                via[used] = via[name]
                frontier.append(used)
        for name, root in sorted(via.items()):
            if not any(node.lineno <= n <= node.end_lineno
                       for node in scopes[name] for n in changed):
                continue
            verb = "guards or refuses" if cls == "guard" else "kills"
            what = "a star import" if name == "*" else name
            doors.append((cls, "%s changes %s, which %s" % (
                path, what, verb) if name == root else
                "%s changes %s, which %s reaches (it %s)" % (
                    path, what, root, verb)))
    return doors


def _bindings(body):
    """(name, statement) for every module-level binding in `body`: a
    function or class, an assignment to a name, and each name an `import`
    or `from ... import` binds (`import a.b` binds `a`; a star import binds
    `*`). A binding inside a module-level if, try, with or loop is still
    module-level, so those blocks are read through; a function or class
    body is its own scope and is not."""
    import ast
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            yield node.name, node
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                yield alias.asname or alias.name.split(".")[0], node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for target in (node.targets if isinstance(node, ast.Assign)
                           else [node.target]):
                if isinstance(target, ast.Name):
                    yield target.id, node
        else:
            yield from _bindings([c for c in ast.iter_child_nodes(node)
                                  if isinstance(c, (ast.stmt,
                                                    ast.excepthandler))])


def door_line(doors):
    """One sentence naming each door class and its first evidence."""
    first = {}
    for cls, why in doors:
        first.setdefault(cls, why)
    return "; ".join("%s (%s)" % (cls, why) for cls, why in first.items())


def _clip_line(text, n=160):
    text = pk.launder(" ".join(str(text or "").split()))
    return text if len(text) <= n else text[:n - 1] + "…"


def plan(verb_kind, sender, recipient, lane, tip, supersedes=None,
         body="", async_because=None, disputes=(), cited_meld=None,
         snap=None, beacon=None, now=None, pair_room_hint=None):
    """-> {"trigger", "action", "lines", "brief", "row", "invite", "refuse",
           "pair"}

    The door's decision for one `dispatch send|add`, before the row exists.
      trigger  T0 | T1 | T2 | None
      action   refuse | cited | async | nudge | auto-open | brief | exempt
      lines    what the door prints
      brief    text to append to the brief (the BAR), or None
      row      fields stamped on the row (`meld_door`, `async_because`)
      invite   (peer, topic) for the door to open after the row exists
      refuse   the refusal text when the door must not write the row
      pair     {"room", "topic"}: the chain's pair meld when the door can
               name it before the row exists (a continuing chain, or a first
               row whose lane names its task: `pair_room_hint`), and the
               topic this send's round opens with (the T1 or T2 topic, else
               None for the plain round)

    `beacon(peer)` answers True/False/None (live / proven absent / could not
    look); it is injected so a test never walks the host's process table.
    Fail-open everywhere except T0: a chain the fold cannot read opens no
    meld and refuses nothing."""
    import time
    from . import dispatches
    now = time.time() if now is None else now
    out = {"trigger": None, "action": None, "lines": [], "brief": None,
           "row": {}, "invite": None, "refuse": None,
           "pair": {"room": pair_room_hint, "topic": None}}
    async_because = str(async_because or "").strip()
    if async_because:
        out["row"]["async_because"] = async_because
    kind = str(verb_kind or "").strip().lower()
    if kind == "build":
        return _plan_t0(out, sender, recipient, lane, body, supersedes,
                        async_because, cited_meld, tip)
    if kind != "review" or not supersedes:
        return out
    if snap is None:
        snap = dispatches.snapshot()     # read only when a chain continues
    info, err = dispatches.chain_rounds(sender, supersedes, tip, snap=snap,
                                        now=now)
    if err or not info:
        return out
    try:                          # the room this round will open in (P2)
        from . import meld_standing
        out["pair"]["room"] = meld_standing.open_for(sender, recipient) \
            or pair_room(info["parent"], (snap or (None,))[0])[0]
    except Exception:                                   # noqa: BLE001
        out["pair"]["room"] = None
    base = {"rounds": info["rounds_after"], "chain": info["chain"][:12]}
    out["row"]["round_preflight"] = round_preflight(info)
    whisper = round_whisper(info["rounds_after"])
    if whisper:
        out["row"]["round_whisper"] = whisper
    # THE CLOSING RE-SEND IS EXEMPT ONLY BELOW THE BLOCK. At three counted
    # rounds the stop rung walls this chain whatever the in-flight tip is, so
    # the door gives the same answer: T2 applies to a re-send at the
    # reviewer's patch tip once the chain's real rounds reach the block.
    closing = info["adopted_patch"] and not info["fan_out"] \
        and info["rounds_after"] >= dispatches.SPIRAL_BLOCK_ROUNDS
    if not info["new_round"] and not closing:
        # THE MIXED CASE: adopting the reviewer's patch is never a round and
        # never held back, but a design or disputed finding beside it still
        # gets its meld offered — for those findings only.
        if info["adopted_patch"]:
            mixed = _plan_t1(dict(out, lines=[], row=dict(out["row"]),
                                  pair=dict(out["pair"])), base,
                             info, recipient, lane, tip, disputes)
            if mixed["trigger"]:
                return mixed
        why = ("the adopted mechanical cure tip" if info["adopted_patch"]
               else "a tip already on this chain, sent to another reader")
        out.update(action="exempt")
        out["row"]["meld_door"] = dict(base, action="exempt", why=why)
        return out
    # THE CURE MUST NOT COUNT AS THE DISEASE, at the door as at the rung: a
    # meld this seat already converged with the reader (recorded on a row,
    # or agreed in the room and not yet recorded) is the bar this round
    # carries out, and opening another would re-litigate it.
    melded = info["melded"]
    # EXACT-ROUND AUTHORITY (falsifier (h)): the door always decides a NEW
    # round, and a pair meld's AGREED is authority for its own round only,
    # so neither a recorded one nor a converged one exempts this send. A
    # meld in a room of its own (a legacy bar meld) keeps its exemption.
    if melded and is_pair_room(melded):
        melded = None
    if not melded and info["rounds_after"] >= 2:
        ok, room = melded_with(recipient, dispatches.SPIRAL_WINDOW_H, sender,
                               now=now, chain=info["chain"],
                               lane=info["lane"], tips=info["tips"],
                               skip_pair=True)
        melded = room if ok else None
    if melded and info["rounds_after"] >= 2:
        out.update(action="exempt")
        out["row"]["meld_door"] = dict(base, action="exempt",
                                       why="bar agreed in " + melded)
        out["lines"].append(
            "helm dispatch: this chain's bar was AGREED in %s; a finding "
            "outside it goes back to that room, not onto a new round"
            % melded)
        return out
    if info["rounds_after"] >= dispatches.SPIRAL_BLOCK_ROUNDS:
        return _plan_t2(out, base, info, recipient, lane, async_because,
                        snap, beacon)
    if info["rounds_after"] == dispatches.SPIRAL_MELD_ROUNDS:
        return _plan_t1(out, base, info, recipient, lane, tip, disputes)
    return out


def _plan_t0(out, sender, recipient, lane, body, supersedes, async_because,
             cited_meld, tip=None):
    if supersedes:
        return out                      # not the FIRST build row of a chain
    hits = irreversible_hits("%s\n%s" % (lane, body))
    if not hits:
        return out
    out["trigger"] = "T0"
    base = {"trigger": "T0", "hits": hits}
    if cited_meld:
        # BOUND EXACTLY AS A VERDICT'S CITATION IS: the meld was between this
        # row's author and its reader, about this lane (a first build row has
        # no chain yet), agreed on this row's tip — and AGREED.
        fields, why = meld_citation(
            cited_meld, {"sender": sender, "recipient": recipient,
                         "lane": lane, "chain_root": None}, (tip,))
        if fields and fields["meld_outcome"] == "agreed":
            out["action"] = "cited"
            out["row"]["meld_door"] = dict(base, action="cited",
                                           room=cited_meld)
            return out
        out["refuse"] = ("helm dispatch: T0 — --meld %s is not an AGREED "
                         "design meld for this row: %s"
                         % (cited_meld, why or "outcome %s"
                            % fields["meld_outcome"]))
        return out
    if async_because:
        out["action"] = "async"
        out["row"]["meld_door"] = dict(base, action="async")
        return out
    out["action"] = "refuse"
    room = out["pair"].get("room")
    out["refuse"] = (
        "helm dispatch: T0 — this build brief names an IRREVERSIBLE target "
        "(%s). Agree the design in one meld BEFORE the first build row%s:\n"
        "  helm chat meld invite %s \"%s\"%s\n%s\n"
        "  Then send again with --meld <room>. If the target is not "
        "irreversible, pass --async-because REASON (ONE argv token) and the "
        "row records why."
        % ("; ".join(hits),
           " (the first round of this task's pair meld)" if room else "",
           recipient, "%s: design meld before an irreversible build%s" % (
               lane, " | " + PAIR_PLAN if room else ""),
           " --into %s" % room if room else "", BAR_TEMPLATE))
    return out


def _plan_t1(out, base, info, recipient, lane, tip, disputes):
    """Round 2: a meld for the design or disputed findings ONLY. The read
    that counts is the one on the round this send continues: every FIX at the
    parent row's tip (a fan-out is one round)."""
    from . import dispatches
    parent_tip = str(info["parent"].get("tip") or "").lower()
    rows = [r for r in info["verdicts"]
            if str(r.get("polarity") or "").casefold() == "fix"
            and str(r.get("reviewed_tip") or "").lower() == parent_tip]
    design = []
    for r in rows:
        design += [str(d) for d in (r.get("design_findings") or ())]
        if r.get("no_patch_because") \
                and not dispatches._has_diff_handoff(r):
            design.append("no cure committed: %s" % r["no_patch_because"])
    disputed = [str(d) for d in disputes or () if str(d).strip()]
    patches = [r.get("patch_tip") for r in rows if r.get("patch_tip")]
    if not design and not disputed:
        return out                      # all-mechanical: never a meld
    out["trigger"], out["action"] = "T1", "nudge"
    out["row"]["meld_door"] = dict(base, trigger="T1", action="nudge",
                                   design=len(design), disputed=len(disputed),
                                   patch=bool(patches))
    topic = "%s: settle the %s finding(s) only%s" % (
        lane, "design and disputed" if design and disputed
        else "design" if design else "disputed", chain_mark(info["chain"]))
    lines = ["helm dispatch: MELD NUDGE (T1) — round %d of chain %s carries "
             "findings a patch cannot settle:" % (base["rounds"], base["chain"])]
    lines += ["  DESIGN: %s" % _clip_line(d) for d in design]
    lines += ["  DISPUTED: %s" % _clip_line(d) for d in disputed]
    room = out["pair"].get("room")
    out["pair"]["topic"] = topic
    if room:
        # THE ROUND THIS SEND OPENS IS THAT MELD (P2): no second room.
        lines.append("  meld on THOSE only, before round 3: this send opens "
                     "them as the next round of the task's pair meld:\n"
                     "    helm chat meld recv %s" % room)
    else:
        lines.append("  meld on THOSE only, before round 3:\n"
                     "    helm chat meld invite %s \"%s\"" % (recipient, topic))
    for patch in patches:
        adopted = str(tip or "").lower() == str(patch).lower() or \
            dispatches._ancestry_authorizes(
                rows[0].get("repo_id"), patch, tip) is True
        lines.append("  the reviewer's mechanical patch %s is %s — nothing "
                     "here holds it back." % (
                         str(patch)[:12],
                         "adopted in this tip" if adopted
                         else "not in this tip; adopting it is never blocked"))
    out["lines"] = lines
    return out


def _plan_t2(out, base, info, recipient, lane, async_because, snap, beacon):
    """Round 3+: open the meld when the reader can join, else brief the bar.

    FINISH IS EXEMPT, as it is advisory at the stop rung: the chain's recorded
    counts are strictly falling and its newest findings are regressions of
    the cure the read before it asked for, so it is converging and a meld
    would re-open it. Door and rung read one fold
    (`dispatches._answered_reading`), so they give the same answer about the
    same chain."""
    under_armed = info["prescription"] == "UNDER-ARMED"
    base = dict(base, trigger="T2", reading=info["prescription"])
    out["trigger"] = "T2"
    if info["prescription"] == "FINISH":
        out["action"] = "finish"
        out["row"]["meld_door"] = dict(base, action="finish",
                                       why=_clip_line(info["evidence"], 200))
        out["lines"].append(
            "helm dispatch: round %d of chain %s — no meld: FINISH, %s"
            % (base["rounds"], base["chain"], _clip_line(info["evidence"])))
        return out
    if async_because:
        out["action"] = "async"
        out["row"]["meld_door"] = dict(base, action="async")
        out["lines"].append(
            "helm dispatch: round %d of chain %s — meld skipped: %s "
            "(recorded on the row)" % (base["rounds"], base["chain"],
                                       async_because))
        return out
    live = beacon(recipient) if beacon else None
    in_flight = _open_rows_for(recipient, snap)
    topic = bar_topic(lane) + chain_mark(info["chain"])
    out["brief"] = BAR_TEMPLATE
    room = out["pair"].get("room")
    # THE BAR IS THIS ROUND'S TOPIC in the task's pair meld (P2), live or
    # not: the difference between auto-open and brief is only whether the
    # reader is expected to answer now.
    out["pair"]["topic"] = topic
    if live is True and not in_flight:
        out["action"] = "auto-open"
        out["invite"] = (recipient, topic)
        out["row"]["meld_door"] = dict(base, action="auto-open")
    else:
        because = ("%s has %d open row(s) in flight" % (recipient, in_flight)
                   if in_flight else
                   "%s has no live beacon" % recipient if live is False
                   else "%s's beacon could not be read" % recipient)
        out["action"] = "brief"
        out["row"]["meld_door"] = dict(base, action="brief", why=because)
        cure = ("helm chat meld recv %s   (the bar is this round's topic "
                "in the task's pair meld)" % room if room else
                'helm chat meld invite %s "%s"' % (recipient, topic))
        out["lines"].append(
            "helm dispatch: round %d of chain %s — no live meld (%s); the "
            "BAR rides this row as its brief. Rows reach every seat. To "
            "converge live instead:\n    %s"
            % (base["rounds"], base["chain"], because, cure))
    if under_armed:
        out["lines"].append(
            "helm dispatch: UNDER-ARMED — every read found what the last "
            "one's arms could not see; the meld's job is the BAR, not the "
            "findings: %s" % _clip_line(info["evidence"], 240))
    return out


def _open_rows_for(recipient, snap):
    """How many OPEN rows name `recipient`: a reader mid-read elsewhere."""
    rows = (snap or (None, None))[0] or {}
    want = str(recipient or "").casefold()
    return sum(1 for r in rows.values()
               if isinstance(r, dict) and r.get("status") == "open"
               and str(r.get("recipient") or "").casefold() == want)


def live_beacon(peer):
    """True / False / None: does `peer` have a live, attributable beacon NOW?
    The strict probe the actuator pays before it types; trouble is None."""
    try:
        from . import seats
        pids, trouble = seats.beacon_procs(peer, strict=True)
    except Exception:                                   # noqa: BLE001
        return None
    if trouble:
        return None
    return bool(pids)


def melded_with(peer, span_h, own, now=None, chain=None, lane=None,
                tips=(), skip_pair=False):
    """(True, room) iff this seat CONVERGED a meld with `peer` inside the
    spiral's own window, else (False, reason).

    #70, and it is the guard punishing a seat for taking the guard's own cure.
    review_spiral counts every distinct reviewed tip and consults NO meld
    state, and the latch fingerprint includes the round count — so the ONE
    post-meld round that IS the convergence re-arms the gate against the seat
    that just did what the block told it to do. The remedy becomes evidence.

    CONVERGED, NOT MERELY OPENED OR CLOSED ALONE: an `active` meld proves
    nothing, and `done` is one side's word. The room must be done/done-mutual,
    hold an accepted chunk from the peer, and every party's last [DONE] must
    carry MELD OUTCOME: AGREED on one tip (review_door.converged).

    INSIDE THE WINDOW, because a meld from last week is not this spiral's cure.
    The caller passes review_spiral's `since_h` (first tip to now); `span_h`
    (first to newest tip) ended at the last round and rejected a meld made
    after it, measured as a whole-suite flake. This asks the question the rule
    exists to ask — did this seat meld DURING this spiral — and says so
    plainly rather than implying a precision it does not have.

    FAILS CLOSED TO NOT-MELDED. An unreadable meld directory suppresses
    nothing: a suppression that fires on absent evidence un-guards the spiral
    rung fleet-wide, which is the same failure the delegation exemption next
    door forbids by name."""
    import glob as _glob
    import os
    import time
    from . import chat, pk
    from .seats_common import SEAT_BYTES, _clip, _scrub
    from .seats_stop_spiral import _my_meld_record
    now = time.time() if now is None else now
    try:
        span_s = max(0.0, float(span_h or 0)) * 3600.0
    except (TypeError, ValueError):
        return False, "unusable span"
    floor = now - span_s
    try:
        paths = _glob.glob(os.path.join(chat.chat_dir(), "*.meld.*.json"))
    except OSError as e:
        return False, "meld state unreadable (%s)" % type(e).__name__
    for path in paths:
        st = pk.read_json(path, None)
        if not isinstance(st, dict):
            continue
        if skip_pair and is_pair_room(st.get("room")):
            continue                      # the door's view: see `plan`
        if not converged(st, peer, chain, lane, tips):
            continue                      # alone, unagreed, elsewhere
        # AND IT MUST BE MY OWN RECORD — see `_my_meld_record`.
        if _my_meld_record(st, own) is not True:
            continue
        try:
            when = float(st.get("epoch") or 0)
        except (TypeError, ValueError):
            continue
        # ONE SECOND OF SLACK, and it is a real boundary rather than a fudge.
        # `epoch` is integer seconds; `floor` is a float. When a spiral's
        # rounds land inside one second — which is exactly the shape of a fast
        # ping-pong, and of every test that stages rounds in a loop — span_h is
        # 0.0 and the window collapses to a POINT that integer truncation puts
        # the meld just outside. The cure would then be rejected for being
        # simultaneous with the disease.
        if when + 1.0 >= floor:
            # LAUNDERED AT THE READ, never at the emit: this room name comes
            # from a meld file ANOTHER seat wrote and _spiral_gate puts it in
            # displayed text. Scrubbing here makes "element two is always safe
            # to display" a contract no future caller can undo by forgetting.
            # SEAT_BYTES because a room name is a glance, like a seat label.
            return True, _clip(_scrub(str(st.get("room") or "?")), SEAT_BYTES)
    return False, "no converged meld with %s inside the window" % peer


def entry_lapsed(room, own, now=None):
    """Did `own` convene `room`, has nobody joined it, and has the entry
    window passed? Read off the convener's own meld state (status `invited`
    until the first READY; `joined_peers` and `spoke_peers` record arrivals).

    A reader that does not join a meld still answers rows, so past the window
    the stop rung stops walling the seat for the reader's absence: the row,
    which carries the BAR, is the conversation (melds-reach-some-seats-rows-
    reach-all). Unreadable state is NOT lapsed: that would only lift a block
    on evidence nobody read."""
    import time
    from . import meld, pk
    # A PAIR ROOM NEVER LAPSES. Every dispatch opens a round in it, so a
    # quiet round is the ordinary state of a task, not a meld its reader
    # declined; only a two-sided exchange speaks for it (`pair_exchange`).
    if is_pair_room(room):
        return False
    now = time.time() if now is None else now
    st = pk.read_json(meld.state_path(room, own), None)
    if not isinstance(st, dict) or st.get("role") != "convener" \
            or st.get("status") != "invited" \
            or st.get("joined_peers") or st.get("spoke_peers"):
        return False
    try:
        epoch = float(st.get("epoch") or 0)
    except (TypeError, ValueError):
        return False
    return bool(epoch) and now - epoch >= entry_window_s()


def lapsed_line(peer, room):
    """The stop rung's sentence for a meld its reader never entered."""
    return ("\n  %s has not joined room %s within the %dm entry window. Rows "
            "reach every seat: the row carries the BAR, so its verdict is the "
            "conversation now — not blocking. Close the room with `helm chat "
            "meld say %s --marker DONE \"<state + next action>\"`."
            % (peer, room, entry_window_s() // 60, room))


# ---------------------------------------------------------------------------
# THE PAIR MELD: one persistent room per task, one round per dispatch
# ---------------------------------------------------------------------------
# Canon premise every-task-runs-as-one-mixed-family-pair-in-one-persistent-
# meld: a task's FIRST dispatch of any kind opens a meld between the row's
# sender and its reader, and its first round is the PLAN. Every later dispatch
# of the chain (a FIX, the cure, the re-read, a rebind to a new reader) opens
# the NEXT ROUND of that same room, so the conversation is already where the
# next reader arrives. The door's own melds (T0, T1, T2) are rounds of it,
# never rooms of their own.
#
# THE NAME IS A PURE FUNCTION OF THE CHAIN, so a second room for one chain
# cannot be minted (falsifier (b)): the task the chain's FIRST row names in
# its lane or note, else the chain root, scoped by the project that owns the
# chain's repository. A chain never leaves its repository, so a reviewer from
# another project is invited into the owner's room.
#
# THE ROW STAYS THE LEDGER. Verdicts, patch tips and the gate live on rows;
# the room carries the conversation and the MELD OUTCOME a verdict records
# with --meld. Nothing here can stop a row: a round that cannot open leaves
# the row as the conversation, exactly as before (falsifier (c)).

PAIR_PREFIX = "meld-0-pair-"
_PAIR_ROOM = re.compile(r"meld-0-pair-[a-z0-9-]{1,48}\Z")
_SCOPE_CHARS = 16

#: What round one agrees, in the premise's order.
PAIR_PLAN = (
    "ROUND 1 IS THE PLAN, agreed before anyone builds: (1) the PROBLEM, its "
    "INVARIANTS and the ACCEPTANCE CHECKS; (2) the SPLIT into genuinely "
    "independent work, and who OWNS the combined result; (3) either side "
    "implements, the other reviews those changes; (4) a correction gets a "
    "focused check of what changed, never a restarted whole review; (5) the "
    "gate lands it: agreement is necessary, never sufficient.")
#: What every later round is for.
PAIR_NEXT_ROUND = (
    "FOCUSED: check what changed since the last round and what it touches, "
    "never a restarted whole review. The plan and the earlier rounds are in "
    "this room.")


def is_pair_room(room):
    """Is `room` a task's pair meld (`meld-0-pair-<scope>-<key>`)?"""
    return bool(_PAIR_ROOM.fullmatch(str(room or "")))


def _chain_root_row(row, current=None):
    """The chain's FIRST row, or None when it cannot be read
    (`taskkey.first_row`, the one reading of it)."""
    from . import taskkey
    return taskkey.first_row(row, current)


def pair_key(row, current=None):
    """(key, why) — the task the chain's FIRST row serves by the one join
    (`taskkey.join`: the task the row records, else exactly one task/N or
    task-N in its lane or note), else `chain/<id12>`; (None, why) when the
    chain cannot be read.

    THE FIRST ROW DECIDES, never the row in hand: a round whose lane was
    renamed, or names another task in passing, stays in the room its chain
    opened. Two tasks named on one first row are ambiguous, and the chain
    root is then the honest key. The lane's record is not read: the room is
    named from the chain alone, so it stays one room for the chain's life."""
    from . import taskkey
    root = _chain_root_row(row, current)
    if root is None:
        return None, ("chain %s is UNKNOWN or unreadable, so its pair meld "
                      "cannot be named" % str((row or {}).get("chain_root")
                                              or "?")[:12])
    # THE FIRST ROW IS IN HAND, so the join is given nothing to look it up
    # in: a row that is not its chain's first reads UNKNOWN here rather than
    # being quietly resolved a second time.
    key = taskkey.join(row=root, current={}, lanes=False)
    if key.task:
        return key.task, None
    chain = str(root.get("id") or root.get("chain_root") or "")
    if not _chain_id(chain):
        return None, "the row has no chain id to key a pair meld on"
    return "chain/" + chain[:12], None


def pair_scope(row, current=None):
    """The owning project's token for the chain's room: the project of the
    repository its FIRST row binds, the one a lane claim is keyed on."""
    import os
    from . import dispatches, pk
    from .work import _lanes
    root = _chain_root_row(row, current) or row or {}
    repo_id = str(root.get("repo_id") or (row or {}).get("repo_id") or "")
    token = None
    if repo_id:
        token = dispatches._repo_project(repo_id) or _lanes.project_token(
            os.path.dirname(repo_id.rstrip(os.sep)))
    raw = str(token or "local")
    scope = pk.slug(raw).strip("-")
    if len(scope) > _SCOPE_CHARS or scope != raw:
        digest = hashlib.blake2b(raw.encode("utf-8"), digest_size=4).hexdigest()
        keep = _SCOPE_CHARS - len(digest) - 1
        scope = "%s-%s" % (scope[:keep].rstrip("-"), digest)
    return scope or "local"


def pair_room(row, current=None):
    """(room, key) — the chain's pair meld, or (None, why)."""
    key, why = pair_key(row, current)
    if key is None:
        return None, why
    room = "%s%s-%s" % (PAIR_PREFIX, pair_scope(row, current),
                        key.replace("/", "-"))
    return room, key


def pairing(sender, recipient, families=None):
    """(label, text): `mixed`, `same` or `unknown`, with provenance.

    A pair of one model family is SAID to be one, never dressed as a mixed
    pair: its room still carries the conversation. What the gate owes is
    one approval-tier read by a reader that is not the author, of any
    family (the owner's ruling, room row 2104), so the label names no other
    family's read as owed. PROVEN means the approval tier proved the
    runtime family. When that proof is absent, a durable roster declaration
    may classify the pair as DECLARED, but the surface says runtime unproven.
    A measured contradiction is DISAGREEMENT, never a declaration fallback."""
    from . import chat, dispatches
    if families is None:
        mine, mine_proof = dispatches._pair_families(sender)
        theirs, their_proof = dispatches._pair_families(recipient)
    else:
        mine, theirs = set(families(sender) or ()), \
            set(families(recipient) or ())
        mine_proof = their_proof = "PROVEN"
    if not mine or not theirs:
        missing = [(s, p) for s, f, p in (
            (sender, mine, mine_proof), (recipient, theirs, their_proof)) if not f]
        return "unknown", ("family %s for %s" % (
            "DISAGREEMENT" if any(p == "DISAGREEMENT" for _s, p in missing)
            else "UNKNOWN", ", ".join(chat._dsan(s) for s, _p in missing)))

    def leg(value, proof):
        text = "+".join(sorted(value))
        return "%s %s" % (text, proof) if proof == "DECLARED" else text

    unproven = [chat._dsan(s) for s, p in (
        (sender, mine_proof), (recipient, their_proof)) if p == "DECLARED"]
    caveat = ("; runtime unproven for %s" % ", ".join(unproven)) \
        if unproven else ""
    if mine & theirs:
        shared = "+".join(sorted(mine & theirs))
        proof = ("; %s, %s%s" % (leg(mine, mine_proof),
                                  leg(theirs, their_proof), caveat)) \
            if unproven else ""
        return "same", ("SAME FAMILY (%s%s): this is NOT a mixed-family pair"
                        % (shared, proof))
    return "mixed", "mixed (%s + %s%s)" % (
        leg(mine, mine_proof), leg(theirs, their_proof), caveat)


def round_preflight(info):
    """The chain facts a pre-lock door decision must not outlive."""
    return (info["chain"], info["rounds_after"], info["new_round"],
            info["adopted_patch"], info["fan_out"], info["prescription"],
            info["evidence"], info["melded"], tuple(info["tips"]))


def round_whisper(rounds):
    """The review-chain nudge, not the pair-room's own round count."""
    if rounds < 3:
        return ""
    return ("Round %d: xfam-reviewer-fixes-its-own-findings — does this reader "
            "fix its findings in this pass? per-case-handler-spiral-cure-is-"
            "whole-object-or-refuse — redesign the whole object or refuse, "
            "not one case at a time. xfam-review-never-blocks-a-reversible-"
            "land — land reversible work after one bounded pass and file later "
            "findings. Ask for the FULL falsifier set for this area so the "
            "next round confirms rather than discovers; is the substrate "
            "wrong, and does state live in the wrong place?" % rounds)


def pair_topic(row, key, rounds, pair_text, door_topic=None, acting=None):
    """The seed of one round: which row it is about, the pairing, and what
    the round is for (the PLAN, the door's topic, or a focused check)."""
    from . import chat
    lane = " ".join(str(row.get("lane") or "").replace("@", "").split())
    chain = _chain_id(row.get("chain_root") or row.get("id")) or ""
    head = ("opening-row=%s opening-chain=%s | %s %s: %s row %s at "
            "%s%s") % (
                str(row["id"])[:12], chain, key, lane[:80],
                row.get("kind") or "dispatch", str(row["id"])[:12],
                str(row.get("tip") or "")[:12], chain_mark(chain))
    parts = [head, "round %d" % (rounds + 1), "pairing: " + pair_text]
    if acting and acting != row.get("sender"):
        parts.append("opened by %s for %s" % (chat._dsan(acting),
                                               chat._dsan(row.get("sender"))))
    if door_topic:
        parts.append(" ".join(str(door_topic).replace("@", "").split()))
    if row.get("round_whisper"):
        parts.append(str(row["round_whisper"]))
    parts.append(PAIR_PLAN if not rounds else PAIR_NEXT_ROUND)
    return " | ".join(parts)


def open_pair_round(row, current=None, door_topic=None, families=None,
                    verb="send", acting=None):
    """Open the next round of `row`'s pair meld, as its sender, before the
    row's ring goes out. -> {"room", "key", "round", "epoch", "topic",
    "pairing", "ring", "lines"} or {"error", "lines"}.

    THE INVITE IS THE RING (P6): the room gets no @mention, and `ring` is the
    text the dispatch's own DM or mention carries instead, so the reader is
    woken once. FAIL-OPEN, always: the row is written and rings whatever this
    returns, because melds reach some seats and rows reach all of them."""
    from . import chat, meld
    row = row or {}
    sender, reader = str(row.get("sender") or ""), str(row.get("recipient")
                                                        or "")
    if not sender or not reader or not row.get("id"):
        return {"error": "the row names no sender and reader to pair",
                "lines": []}
    standing = _standing_round(row, current, sender, reader, families, verb,
                               acting)
    if standing is not None:
        return standing
    room, key = pair_room(row, current)
    if room is None:
        return {"error": key, "lines": []}
    try:
        label, text = pairing(sender, reader, families)
        opened = {}

        def topic_for(rounds):
            return pair_topic(row, key, rounds, text, door_topic, acting)
        row12 = str(row["id"])[:12]
        ring = "dispatch row %s (%s)" % (row12, verb)
        meld.invite(reader, "pending locked round topic", seat=sender,
                    room=room, ring=ring, _round_topic=topic_for,
                    _opened=opened, _round_marker="opening-row=%s " % row12)
        rounds = opened["prior_rounds"]
        topic = opened["topic"]
        epoch = opened["epoch"]
    except (SystemExit, Exception) as exc:            # noqa: BLE001
        return {"error": "%s: %s" % (type(exc).__name__, exc), "room": None,
                "lines": ["the pair meld round could not open (%s: %s); the "
                          "row is the conversation, as before"
                          % (type(exc).__name__, exc)]}
    n = rounds + 1
    what = (PAIR_PLAN.split(",")[0].lower() if n == 1 else
            "a focused check of what changed; the earlier rounds are in the "
            "room")
    lines = ["your pair meld for this task: %s (%s, round %d; you and @%s; "
             "pairing: %s)" % (room, key, n, chat._dsan(reader), text),
             "  helm chat meld recv %s   (returns when @%s joins; %s)"
             % (room, chat._dsan(reader), what)]
    ring_text = (
        "PAIR MELD for %s: %s (round %d, with %s; pairing: %s). The row is "
        "the ledger and reaches you whether or not you join. To work it "
        "live: helm chat meld join %s, then helm chat meld recv %s. Joining "
        "prints the earlier rounds as a digest bounded to your window; the "
        "whole log is helm chat read --room %s."
        % (key, room, n, chat._dsan(sender), text, room, room, room))
    return {"room": room, "key": key, "round": n, "epoch": epoch,
            "topic": topic, "pairing": label, "ring": ring_text,
            "lines": lines}


def _standing_round(row, current, sender, reader, families, verb, acting):
    """The round of `row` in its pair's STANDING room (task/3560), or None
    when the pair has none open and the per-chain pair meld opens as before.

    A working pair that keeps one standing room runs every task through it:
    the round is one `[STANDING-ROUND]` row there, and no per-chain room is
    minted. The key still names the task or chain, so the round says which
    one it is about. FAIL-OPEN: any failure here is None, and the pair meld
    opens exactly as it did."""
    from . import chat, meld_standing
    try:
        room = meld_standing.open_for(sender, reader)
        if room is None:
            return None
        key, _why = pair_key(row, current)
        key = key or "row/" + str(row["id"])[:12]
        label, text = pairing(sender, reader, families)
        n, _reused = meld_standing.post_round(room, row, verb, key, text,
                                              acting=acting)
    except Exception:                                   # noqa: BLE001
        return None
    lines = ["your STANDING room with @%s carries this task: %s (%s, round "
             "%d there; pairing: %s; no per-task room)"
             % (chat._dsan(reader), room, key, n, text),
             "  helm chat meld say %s \"...\"   (never blocks; wakes @%s)"
             % (room, chat._dsan(reader)),
             "  helm chat meld recv %s   (returns at once)" % room]
    ring_text = (
        "STANDING ROOM for %s: %s (round %d there, with %s; pairing: %s). "
        "The row is the ledger and reaches you whether or not you read it. "
        "Talk it through there: helm chat meld recv %s returns at once, "
        "helm chat meld say %s \"...\" wakes the others."
        % (key, room, n, chat._dsan(sender), text, room, room))
    return {"room": room, "key": key, "round": n, "epoch": None,
            "topic": None, "pairing": label, "ring": ring_text,
            "lines": lines, "standing": True}


def pair_room_if_open(row, current=None):
    """The chain's pair meld when it has opened at least one round, else
    None — the test a rebind asks before it continues a conversation."""
    from . import chat, meld
    room, _key = pair_room(row, current)
    if room is None:
        return None
    try:
        rows, _total = chat.read(room)
    except Exception:                                   # noqa: BLE001
        return None
    return room if meld.seeds(rows) else None


def _lifecycle_pair_rooms():
    """Every pair room with a meld lifecycle journal, read off the journal
    directory's names (one file a room) rather than the chat directory, whose
    listing is paid on every stop and holds every cursor on the bus."""
    import base64
    import os
    from . import chat, meld
    root = os.path.join(chat.chat_dir(), meld._LIFECYCLE_DIR)
    head = base64.urlsafe_b64encode(PAIR_PREFIX.encode()).decode()
    try:
        names = os.listdir(root)
    except OSError:
        return []
    out = []
    for name in names:
        if not name.startswith(head) or not name.endswith(".jsonl"):
            continue
        try:
            room = meld._room_from_key(name[:-len(".jsonl")])
        except meld.LifecycleError:
            continue
        if is_pair_room(room):
            out.append(room)
    return sorted(out)


def pair_turns_owed(seat):
    """[{room, epoch, peer, marker, at}] — pair-meld rounds whose floor is
    THIS seat's: the peer yielded to it, the reader joined and the convener
    has not spoken, or the peer closed and this seat has not.

    Only this seat's own round state is read, and only its CURRENT round: a
    round the room has moved past is over, however it ended. A seat that was
    rung about a pair meld and never joined it owes nothing here; the row
    reaches it as any row does (falsifier (c))."""
    from . import chat, meld, pk
    out = []
    for room in _lifecycle_pair_rooms():
        st = pk.read_json(meld.state_path(room, seat), None)
        if not isinstance(st, dict) or str(st.get("self") or "") != seat:
            continue
        if st.get("status") not in ("invited", "active", "peer-done"):
            continue
        epoch = st.get("epoch")
        rows, _total = chat.read(room)
        latest, _conv, _text = meld.latest_seed(rows)
        if latest is None or latest != epoch:
            continue                      # a round the room moved past
        members = set(st.get("peers") or ()) | {seat}
        last = None
        for i, m in enumerate(rows):
            text, frm = m.get("text") or "", str(m.get("from") or "")
            if m.get("react") or frm not in members:
                continue
            em = meld._EPOCH_RE.search(text)
            if not em or int(em.group(1)) != epoch:
                continue
            mk = meld._MARKER_RE.search(text)
            if mk:
                last = (i, frm, mk.group(1))
            elif meld._READY_RE.search(text):
                last = (i, frm, "READY")
        if last and last[1] != seat and last[2] in ("YIELD", "READY", "DONE"):
            out.append({"room": room, "epoch": epoch, "peer": last[1],
                        "marker": last[2], "at": last[0]})
    return out


def pair_exchange(seat, peer, chain, span_h, now=None):
    """The pair room of `chain` in which BOTH `seat` (the author) and `peer`
    (the chain's CURRENT reader) took a turn inside the spiral's window, or
    None. The integrator's ruling for the spiral rung: a pair round counts as
    the conversation the rung asks for only then.

    A TURN is a floor chunk (YIELD, HOLD or DONE) the seat posted itself: a
    round's seed, its invite and a READY are what the dispatch and the meld
    verbs post, so an auto-opened round that nobody spoke in is not a
    conversation, however long it has sat. A turn from a reader the chain
    has since moved off is not the current reader's. The room belongs to
    the chain when its name ends in the chain id or a round's generated opening
    field binds that chain. Free text never supplies that authority. The caller
    still owes the reader's live beacon."""
    import os
    import time
    from . import chat, meld, pk
    chain12 = _chain_id(chain)
    if not chain12 or not seat or not peer:
        return None
    now = time.time() if now is None else now
    try:
        floor = now - max(0.0, float(span_h or 0)) * 3600.0 - 1.0
    except (TypeError, ValueError):
        return None
    for room in _lifecycle_pair_rooms():
        if not os.path.exists(meld.state_path(room, seat)):
            continue
        rows, _total = chat.read(room)
        room_is_chain = room.endswith("-chain-" + chain12)
        rounds = {ep for ep, convener, seed, _i in meld.seeds(rows)
                  if (room_is_chain or _seed_chain(seed) == chain12)
                  and {seat, peer} <= ({convener} |
                      set(meld._invited_seats(seed)))}
        if not rounds:
            continue
        spoke = {ep: set() for ep in rounds}
        for m in rows:
            text, frm = m.get("text") or "", str(m.get("from") or "")
            em = meld._EPOCH_RE.search(text)
            if m.get("react") or frm not in (seat, peer) \
                    or not em or int(em.group(1)) not in rounds \
                    or meld._SEED_RE.match(text) \
                    or not meld._MARKER_RE.search(text):
                continue
            at = pk.parse_ts_epoch(m.get("ts"))
            if at is not None and at >= floor:
                spoke[int(em.group(1))].add(frm)
        if any({seat, peer} <= turns for turns in spoke.values()):
            return room
    return None


def pair_turn_line(owed):
    """The stop rung's sentence for the rounds this seat owes a turn in."""
    from . import chat
    what = {"YIELD": "yielded the floor to you",
            "READY": "joined and is waiting for your first chunk",
            "DONE": "closed; your closing chunk (with its MELD OUTCOME) is owed"}
    lines = ["[helm stop-guard] your pair meld owes a turn:"]
    for o in owed:
        lines.append("  %s round e:%s — @%s %s:\n    helm chat meld recv %s   "
                     "then   helm chat meld say %s --marker YIELD|DONE \"...\""
                     % (o["room"], o["epoch"], chat._dsan(o["peer"]),
                        what.get(o["marker"], o["marker"]), o["room"],
                        o["room"]))
    lines.append("Blocks once per set of owed turns. The row is the ledger "
                 "and stays owed either way; HELM_STOP_GUARD_PAIR=0 disables.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# THE FALSIFIERS, measured (docs/MELD_REVIEW_DOOR.md)
# ---------------------------------------------------------------------------
#   (a) T2's reader join rate within the entry window under 50% means T2's
#       shape is wrong: the door opens melds seats do not enter.
#   (b) T1 firing after an all-mechanical patch FIX means T1's scope is wrong.
T2_JOIN_FLOOR = 0.5


def _t2_rooms(rows_by_chain, sender):
    """{chain12: [(room, epoch, state)]} for the melds `sender` convened
    whose topic names a chain: the door opens T2 melds as
    '<lane>: agree the bar (...) (chain <chain12>)'."""
    import glob
    import os
    from . import chat, pk
    out = {}
    pattern = os.path.join(chat.chat_dir(), "meld-*.meld.*.json")
    for path in glob.glob(pattern):
        st = pk.read_json(path, None)
        if not isinstance(st, dict) or st.get("role") != "convener":
            continue
        if str(st.get("self") or "").casefold() != str(sender).casefold():
            continue
        room = str(st.get("room") or "")
        found = re.search(r"-chain-([0-9a-f]{12})$", room)
        key = found.group(1) if found else None
        if key is None:
            rows, _total = chat.read(room)
            seed = next((m.get("text") or "" for m in rows
                         if meld_seed(m)), "")
            found = _CHAIN_MARK.search(seed)
            key = found.group(1) if found else None
        if key and key in rows_by_chain:
            out.setdefault(key, []).append((room, st))
    return out


def meld_seed(row):
    from . import meld
    return bool(meld._EPOCH_RE.search(row.get("text") or "")
                and row.get("from"))


def joined_within(room, st, window_s):
    """Did the invited reader answer READY within `window_s` of the seed?
    -> True / False / None (the room could not be read)."""
    import calendar
    import time
    from . import chat, meld
    rows, total = chat.read(room)
    if not total:
        return None
    try:
        epoch = float(st.get("epoch") or 0)
    except (TypeError, ValueError):
        return None
    peers = {str(p) for p in (st.get("peers") or [st.get("peer")]) if p}
    for m in rows:
        if str(m.get("from") or "") not in peers:
            continue
        if not (meld._READY_RE.search(m.get("text") or "")
                or meld._MARKER_RE.search(m.get("text") or "")):
            continue
        # THIS ROUND'S ANSWER ONLY. A pair meld's room holds every round of
        # its task, and a READY from round one is not a join of round three.
        em = meld._EPOCH_RE.search(m.get("text") or "")
        if em and float(em.group(1)) != epoch:
            continue
        try:
            when = calendar.timegm(time.strptime(
                str(m.get("ts") or "")[:19], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            continue
        return when - epoch <= window_s
    return False


def _mode_ts(value):
    """One dispatch instant as epoch seconds, or None. The ledger and this
    report share `dispatches.instant_epoch`; a malformed stamp is UNKNOWN, not
    an invented ordering."""
    from . import dispatches
    return dispatches.instant_epoch(value)


def _mode_chain_rows(current, chain, cancelled=False):
    """Review rows on one real chain root, in genesis append order. A
    cancelled row is left out unless `cancelled` asks for it."""
    return [r for r in (current or {}).values() if isinstance(r, dict)
            and r.get("kind") == "review"
            and r.get("chain_root") == chain
            and (cancelled or r.get("status") != "cancelled")]


def _source_clean_claim(event, row):
    """An accepted clean hold answers only when this row's reader held it.

    The fold can take a hold but drop its clean tip (owner-gated or unreadable),
    and a forged hold from another seat can install a tip without becoming the
    reader's answer. Use the door's own reader comparator for both questions."""
    from . import dispatches
    return event.get("event") == "hold" \
        and event.get("owner_gated") is not True \
        and dispatches._reader_clean(dict(event, recipient=row.get("recipient")))


def _first_source_clean_hold(rows, accepted):
    """The first accepted source-clean hold on these rows: (ts, epoch).

    RELEASE/RE-HOLD DOES NOT REWRITE HISTORY. The first hold is the trial's
    stop clock; a later release says work resumed, and a later hold is another
    event, not a new answer to when the chain first reached clean. Only events
    the canonical fold accepted are input, so a refused/forged hold never ends
    the interval, and only a hold whose claim the fold kept is source-clean.
    The row need not still be held now: a later release, retraction or cancel
    rewrites no hold, so the caller passes the chain's cancelled rows too."""
    found = []
    for order, row in enumerate(rows):
        for event_order, event in enumerate((accepted or {}).get(row["id"], ())):
            if not _source_clean_claim(event, row):
                continue
            when = _mode_ts(event.get("ts"))
            if when is not None:
                found.append((when, order, event_order, event.get("ts")))
    if not found:
        return None, None
    first = min(found)
    return first[3], first[0]


def mode_metrics(current, accepted, cutoff=0):
    """Read-only PATCH/MELD-DIFF trial metrics, one row per chain root.

    MODE and ENROLLMENT come from recorded rows. Enrollment is the first
    distinct chain tip carrying the mode; fan-out shares its distinct-tip
    round. ACTIVE MODE ROUNDS and CURE CYCLES belong to mode-stamped rows and
    later rows addressed to their reader seat, even when a reissue has no mode.
    A later independent reader's fixture cure is not charged to that seat
    (the measured task/3693 shape).

    FIRST SEND -> HOLD is the first mode-enrolled review dispatch to the
    chain's first accepted source-clean hold, read from the fold's accepted
    event slice. A later cancel rewrites neither end, so both clocks read the
    chain's cancelled rows. It never reads a separate meld room or infers
    AGREED from one.

    TOKENS ARE EXPLICITLY UNKNOWN today. The proxy meter records seat and
    request, but no dispatch/chain binding (`proxy_usage`'s contract), and the
    dispatch events capture no token total. Brief or meld BYTES are not tokens.
    Keeping the UNKNOWN on every chain is the usable metric: a future bound
    producer can replace it without laundering an estimate into the trial.

    UNMEASURED IS UNKNOWN, NEVER 0. A chain whose rows record two modes has no
    enrolled reader, and `accepted` None means the event slices were not read:
    either way its active rounds and cure cycles were never counted. A row
    this helm cannot read in full (`dispatches.unknown_event_kinds`) took no
    event after the kind it has no arm for: an enrolled one leaves the rounds
    and cures uncounted, and any one can hide the first hold.
    """
    from . import dispatches
    chains = []
    roots = []
    for row in (current or {}).values():
        if not isinstance(row, dict) or row.get("kind") != "review":
            continue
        mode = dispatches._review_mode_of(row)
        chain = row.get("chain_root")
        when = _mode_ts(row.get("ts"))
        if mode not in dispatches.REVIEW_MODE_LINES or not chain \
                or chain == dispatches.CHAIN_UNKNOWN or when is None \
                or when < cutoff or chain in roots:
            continue
        roots.append(chain)
    for chain in roots:
        rows = _mode_chain_rows(current, chain, cancelled=True)
        modes = {dispatches._review_mode_of(r) for r in rows
                 if dispatches._review_mode_of(r) in dispatches.REVIEW_MODE_LINES}
        mode = next(iter(modes)) if len(modes) == 1 else "UNKNOWN"
        enrolled = [r for r in rows if dispatches._review_mode_of(r) == mode]
        tips = {}
        for order, row in enumerate(rows):
            tips.setdefault(str(row.get("tip") or ""), order)
        enrolled.sort(key=lambda r: tips.get(str(r.get("tip") or ""), 0))
        ordered_tips = sorted(tips, key=tips.get)
        enrollment = ordered_tips.index(str(enrolled[0].get("tip") or "")) + 1 \
            if enrolled else "UNKNOWN"
        # THE ENROLLED READER IS A SEAT, NOT A STAMP. `add`, `rebind` and
        # `retract --reissue` mint a row without review guidance (task/3713
        # D4), so the reader's reissued FIX carries no mode; read as an
        # independent reader's, a retracted FIX's reissue left the chain at 0
        # cures. From the chain's first mode
        # row on, a row sent to a seat the mode was recorded for is its work.
        ids = {r["id"] for r in enrolled}
        seats = {str(r.get("recipient") or "").casefold() for r in enrolled}
        start = next((i for i, r in enumerate(rows) if r["id"] in ids),
                     len(rows))
        enrolled = [r for r in rows[start:] if r["id"] in ids
                    or str(r.get("recipient") or "").casefold() in seats]

        # ONLY THE ENROLLED MODE'S ANSWERS. An independent reader can FIX the
        # adopted patch tip (the task/3693 fixture follow-up); that does not turn
        # the enrolled reader's confirmation send into another mode round.
        # Cancelled rows keep their recorded mode and accepted answers.
        known = mode != "UNKNOWN" and accepted is not None \
            and not any(dispatches.unknown_event_kinds(r) for r in enrolled)
        answered, fixes, patches, diff_parents = set(), set(), set(), {}
        for row in enrolled if known else ():
            row_tip = str(row.get("tip") or "")
            for event in (accepted or {}).get(row["id"], ()):
                if _source_clean_claim(event, row):
                    answered.add(row_tip)
                # The accepted slice keeps the old verdict after retraction;
                # only its projected row says that verdict no longer counts.
                if event.get("event") != "verdict" or row.get("verdict_retracted"):
                    continue
                answered.add(str(event.get("reviewed_tip") or row_tip))
                if str(event.get("polarity") or "").casefold() == "fix":
                    fixes.add(str(event.get("reviewed_tip") or row_tip))
                    if event.get("patch_tip"):
                        patches.add(str(event["patch_tip"]).lower())
                    elif dispatches._review_mode_of(row) == "MELD-DIFF" \
                            and dispatches._has_diff_handoff(event):
                        diff_parents[row["id"]] = (row, event)
        # Mirror the spiral fold's advancing, direct MELD-DIFF successor,
        # but take FIX evidence from accepted events for this enrolled reader.
        # An independent fixture FIX at the cure tip cannot add a mode round.
        first_tips = {str(r.get("tip") or "").lower(): r["id"]
                      for r in reversed(rows)}
        diff_tips, seen = set(), set()
        for row in rows:
            match = diff_parents.get(str(row.get("supersedes") or ""))
            parent, fix = match if match else (None, None)
            tip = str(row.get("tip") or "")
            sent = _mode_ts(row.get("ts"))
            fixed = _mode_ts(fix.get("ts")) if fix else None
            if not parent or parent["id"] in seen or not tip \
                    or sent is None or fixed is None or sent < fixed \
                    or (sent == fixed and not dispatches._has_applied_diff(row, parent)) \
                    or tip.lower() == str(parent.get("tip") or "").lower() \
                    or row.get("repo_id") != parent.get("repo_id") \
                    or first_tips[tip.lower()] != row["id"]:
                continue
            seen.add(parent["id"])
            if dispatches._has_applied_diff(row, parent):
                diff_tips.add(tip)
        adopted = (patches | diff_tips) - fixes
        active = answered - adopted
        cures = fixes
        # A CANCEL REWRITES NO HOLD AND NO SEND, any more than a release does:
        # both clocks read the chain's cancelled rows too. A send clock that
        # dropped a cancelled row kept its hold and lost its send, so the
        # clock started at a later fan-out send, after the dispatch that hold
        # answered.
        first = min(((_mode_ts(r.get("ts")), r.get("ts")) for r in rows
                     if dispatches._review_mode_of(r) == mode
                     and _mode_ts(r.get("ts")) is not None),
                    default=(None, None))
        hold_ts, held = (None, None) \
            if any(dispatches.unknown_event_kinds(r) for r in rows) \
            else _first_source_clean_hold(rows, accepted)
        elapsed = int(held - first[0]) if held is not None and first[0] is not None \
            and held >= first[0] else "UNKNOWN"
        chains.append({"chain": chain, "mode": mode,
                       "enrollment_round": enrollment,
                       "active_review_rounds": len(active) if known
                       else "UNKNOWN",
                       "cure_cycles": len(cures) if known else "UNKNOWN",
                       "first_send_ts": first[1], "first_hold_ts": hold_ts,
                       "send_to_hold_s": elapsed,
                       "reviewer_tokens": "UNKNOWN",
                       "author_tokens": "UNKNOWN"})
    by_mode = {}
    for mode in tuple(dispatches.REVIEW_MODE_LINES) + ("UNKNOWN",):
        mine = [r for r in chains if r["mode"] == mode]
        if not mine:
            continue
        elapsed = [r["send_to_hold_s"] for r in mine
                   if isinstance(r["send_to_hold_s"], int)]
        cycles = [r["cure_cycles"] for r in mine]
        by_mode[mode] = {"chains": len(mine),
                         "cure_cycles": sum(cycles) if all(
                             isinstance(c, int) for c in cycles)
                         else "UNKNOWN",
                         "median_send_to_hold_s": _median(elapsed)
                         if elapsed else "UNKNOWN",
                         "reviewer_tokens": "UNKNOWN",
                         "author_tokens": "UNKNOWN"}
    return {"chains": chains, "by_mode": by_mode,
            "token_reading": ("UNKNOWN — no dispatch/chain binding exists for "
                              "token records; bytes are never tokens")}


def census(snap=None, hours=168, now=None):
    """The two falsifiers and mode trial, measured from one ledger snapshot.

    -> {"window_h", "t2": {...}, "t1": {...}, "retro": {...},
        "mode_ab": {...}}
    A falsifier with no rows is UNMEASURED, never HOLDS: absence of firings
    is not evidence about a shape. The state and accepted event slices share
    one strict ledger read, so a release/hold append cannot straddle them."""
    import calendar
    import time
    from . import dispatches
    now = time.time() if now is None else now
    if snap is None:
        current, _events, accepted, _verdicts, unavailable = \
            dispatches.snapshot_and_events()
    elif len(snap) == 5:
        current, _events, accepted, _verdicts, unavailable = snap
    else:
        # A (state, unavailable) snap carries no accepted events, so the mode
        # trial's event-derived counts read UNKNOWN rather than an empty 0.
        current, unavailable = snap
        accepted = None
    if unavailable:
        return {"error": "dispatch ledger unavailable (%s)" % unavailable}
    cutoff = now - hours * 3600
    rows = []
    for r in (current or {}).values():
        try:
            when = calendar.timegm(time.strptime(str(r.get("ts") or ""),
                                                 "%Y-%m-%dT%H:%M:%SZ"))
        except ValueError:
            continue
        if when >= cutoff:
            rows.append(r)
    # (a) T2 join rate
    opened = [r for r in rows if (r.get("meld_door") or {}).get("action")
              == "auto-open"]
    by_chain = {}
    for r in opened:
        by_chain.setdefault(str(r.get("chain_root") or "")[:12], []).append(r)
    window = entry_window_s()
    joined = unreadable = 0
    rooms = {}
    for sender in {str(r.get("sender") or "") for r in opened}:
        for key, found in _t2_rooms(by_chain, sender).items():
            rooms.setdefault(key, []).extend(found)
    measured = 0
    for key, found in rooms.items():
        for room, st in found:
            got = joined_within(room, st, window)
            if got is None:
                unreadable += 1
                continue
            measured += 1
            joined += bool(got)
    rate = (joined / measured) if measured else None
    t2 = {"opened_rows": len(opened), "rooms_measured": measured,
          "joined_in_window": joined, "unreadable": unreadable,
          "entry_window_s": window, "join_rate": rate,
          "reading": "UNMEASURED (no door-opened meld in the window)"
          if rate is None else ("FALSIFIED: T2's shape is wrong"
                                if rate < T2_JOIN_FLOOR else "HOLDS")}
    # (b) T1 on an all-mechanical patch FIX
    fired = [r for r in rows if (r.get("meld_door") or {}).get("trigger")
             == "T1"]
    wrong = [r["id"] for r in fired if _all_mechanical_parent(r, current)
             and not (r.get("meld_door") or {}).get("disputed")]
    t1 = {"fired": len(fired), "on_all_mechanical": len(wrong),
          "rows": [w[:12] for w in wrong],
          "reading": "UNMEASURED (T1 has not fired in the window)"
          if not fired else ("FALSIFIED: T1's scope is wrong" if wrong
                             else "HOLDS")}
    return {"window_h": hours, "t2": t2, "t1": t1,
            "retro": _retro(rows, current),
            "prior": _prior_join_rate(cutoff, window),
            "pair": pair_census(rows, current),
            "mode_ab": mode_metrics(current, accepted, cutoff)}


def _prior_join_rate(cutoff, window):
    """Falsifier (a)'s prior: of EVERY meld convened in the window (door or
    hand), how many had the reader join within the entry window. The door's
    own rate is the falsifier; this is the base rate it is judged against."""
    import glob
    import os
    from . import chat, pk
    convened = joined = unreadable = 0
    for path in glob.glob(os.path.join(chat.chat_dir(), "meld-*.meld.*.json")):
        st = pk.read_json(path, None)
        if not isinstance(st, dict) or st.get("role") != "convener":
            continue
        try:
            if float(st.get("epoch") or 0) < cutoff:
                continue
        except (TypeError, ValueError):
            continue
        got = joined_within(str(st.get("room") or ""), st, window)
        if got is None:
            unreadable += 1
            continue
        convened += 1
        joined += bool(got)
    return {"convened": convened, "joined_in_window": joined,
            "unreadable": unreadable,
            "join_rate": (joined / convened) if convened else None}


def _parent_fixes(row, current):
    parent = (current or {}).get(str(row.get("supersedes") or ""))
    if not parent:
        return []
    tip = str(parent.get("tip") or "").lower()
    chain = parent.get("chain_root")
    return [r for r in (current or {}).values()
            if r.get("chain_root") == chain and chain
            and str(r.get("reviewed_tip") or "").lower() == tip
            and str(r.get("polarity") or "").casefold() == "fix"]


def _all_mechanical_parent(row, current):
    """Every FIX on the round this row continues carries a patch and names no
    design finding and no reason for having no cure."""
    fixes = _parent_fixes(row, current)
    return bool(fixes) and all(
        f.get("patch_tip") and not f.get("design_findings")
        and not f.get("no_patch_because") for f in fixes)


def _retro(rows, current):
    """Before the door recorded anything: what the review sends in the window
    followed, measured on the verdicts they continued — how often T1 would
    have offered a meld (a design-class FIX) and how often it stays silent
    (an all-mechanical patch FIX). Disputes were never recorded before the
    door, so this replay cannot see them."""
    sends = [r for r in rows if r.get("kind") == "review"
             and r.get("supersedes") and r.get("status") != "cancelled"]
    mech = design = unread = 0
    for r in sends:
        fixes = _parent_fixes(r, current)
        if not fixes:
            unread += 1
        elif _all_mechanical_parent(r, current):
            mech += 1
        elif any(f.get("design_findings") or f.get("no_patch_because")
                 for f in fixes):
            design += 1
    return {"continuing_sends": len(sends),
            "after_all_mechanical_fix_t1_silent": mech,
            "after_design_class_fix_t1_offers": design,
            "after_no_fix": unread}


# ---------------------------------------------------------------------------
# THE PAIR MELD'S FALSIFIERS, measured: (a) and (g)
# ---------------------------------------------------------------------------
#: (a) The program's bar: at least this share of tasks whose pair meld opened
#: reach a typed AGREED on a row, measured over at least PAIR_BAR_TASKS tasks.
PAIR_AGREED_FLOOR = 0.6
PAIR_BAR_TASKS = 20


def _median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    mid = len(xs) // 2
    return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) // 2


def _brief_bytes(rows):
    return sum(r.get("brief_bytes") for r in rows
               if isinstance(r.get("brief_bytes"), int)
               and not isinstance(r.get("brief_bytes"), bool))


def pair_census(rows, current):
    """(a) and (g) of the pair meld, read-only, off the ledger and the rooms.

    (a) TASKS: every pair room a row in the window belongs to that opened at
    least one round (its seeds are on the bus, or retirement archived it),
    and how many of them reached a typed AGREED ON A ROW (`meld_outcome` =
    agreed with `meld_room` naming that room; a room agreement nobody
    recorded is not counted). Under PAIR_BAR_TASKS tasks it reads
    UNMEASURED; the bar is PAIR_AGREED_FLOOR.

    (g) COST, in bytes as the proxy for tokens: a chain converged through its
    pair meld costs the room's bytes recorded on the AGREED row
    (`meld_bytes`) plus its rows' brief bytes; a chain converged on rows
    alone (an APPROVE with no agreed meld) costs its brief bytes. The two
    medians are the comparison the program is judged on."""
    from . import chat, chatdebris, meld
    current = current or {}
    try:
        retired = chatdebris.retired_through()
    except Exception:                                   # noqa: BLE001
        retired = {}
    from . import taskkey
    by_room, key_of = {}, {}
    for r in rows:
        try:
            room, key = pair_room(r, current)
        except Exception:                               # noqa: BLE001
            room = key = None
        if room:
            by_room.setdefault(room, set()).add(r.get("chain_root") or r["id"])
            key_of.setdefault(room, (key, _chain_root_row(r, current)))
    opened, agreed, pairings = [], [], {}
    pair_cost, row_cost = [], []
    # RULING 7: how many opened rooms fell back to the chain root, and why,
    # so the 20-task run measures what the task/N parse misses.
    keys = {"task": 0, "chain_fallback": 0, "no_task": 0, "two_tasks": 0}
    for room, chains in sorted(by_room.items()):
        try:
            rrows, _total, fault = chat.read_checked(room, 0)
        except Exception:                               # noqa: BLE001
            rrows, fault = [], "unreadable"
        found = meld.seeds(rrows) if not fault else []
        if not found and room not in retired:
            continue
        opened.append(room)
        key, root = key_of.get(room, (None, None))
        if str(key or "").startswith("task/"):
            keys["task"] += 1
        else:
            keys["chain_fallback"] += 1
            named = taskkey.join(row=root or {}, current=current,
                                 lanes=False).cited
            keys["two_tasks" if len(named) > 1 else "no_task"] += 1
        label = "unread"
        if found:
            m = re.search(r"pairing: (mixed|SAME FAMILY|family UNKNOWN)",
                          found[0][2])
            label = {"mixed": "mixed", "SAME FAMILY": "same",
                     "family UNKNOWN": "unknown"}.get(
                         m.group(1) if m else "", "unknown")
        pairings[label] = pairings.get(label, 0) + 1
        mine = [r for r in current.values() if isinstance(r, dict)
                and (r.get("chain_root") or r.get("id")) in chains]
        hits = [r for r in mine if r.get("meld_outcome") == "agreed"
                and r.get("meld_room") == room]
        if hits:
            agreed.append(room)
            recorded = [r["meld_bytes"] for r in hits
                        if isinstance(r.get("meld_bytes"), int)]
            pair_cost.append((max(recorded) if recorded else
                              sum(len((m.get("text") or "").encode("utf-8"))
                                  for m in rrows)) + _brief_bytes(mine))
    for chain in {r.get("chain_root") or r["id"] for r in rows}:
        mine = [r for r in current.values() if isinstance(r, dict)
                and (r.get("chain_root") or r.get("id")) == chain]
        if any(str(r.get("polarity") or "").casefold() == "approve"
               for r in mine) and not any(
                   r.get("meld_outcome") == "agreed" for r in mine):
            row_cost.append(_brief_bytes(mine))
    rate = len(agreed) / len(opened) if opened else None
    if len(opened) < PAIR_BAR_TASKS:
        reading = ("UNMEASURED (%d of the %d tasks the bar is judged over)"
                   % (len(opened), PAIR_BAR_TASKS))
    elif rate < PAIR_AGREED_FLOOR:
        reading = "FALSIFIED: under %d%% of tasks reached AGREED on a row" \
            % int(PAIR_AGREED_FLOOR * 100)
    else:
        reading = "HOLDS"
    return {"tasks": len(opened), "agreed": len(agreed), "rate": rate,
            "floor": PAIR_AGREED_FLOOR, "bar_tasks": PAIR_BAR_TASKS,
            "reading": reading, "pairings": pairings, "keys": keys,
            "rooms_agreed": [r for r in agreed],
            "cost": {"pair_converged": len(pair_cost),
                     "pair_median_bytes": _median(pair_cost),
                     "row_only_converged": len(row_cost),
                     "row_only_median_bytes": _median(row_cost)}}
