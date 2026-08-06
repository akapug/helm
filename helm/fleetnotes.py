#!/usr/bin/env python3
"""helm fleetnotes — the fleet's durable notes TO THE OWNER.

WHY THIS EXISTS. Agents already wrote the owner's "what happened overnight"
answers: three cells named `reboot-recovery`, `signing-status` and
`cd-open-lanes` on the web cockpit's multiplayer demo board. Two things were
wrong with that home and neither was the writing.

  IT WAS TMPFS. The demo board is the blind relay's log under
  /dev/shm/helm-multiplayer — disposable BY DESIGN, because durable history is
  a client's concern and the relay is a transport. So the owner's overnight
  answers died on the next reboot, which is precisely the event they answer
  questions about.

  IT WAS BEHIND A WORD NOBODY OWNS. They lived on a nav tab called "cave" —
  borrowed from dregg, where a cave is the ATTESTATION NODE, which is what the
  helm cockpit's LEDGER tab shows one tab over. Two meanings, adjacent tabs;
  helm canon already says "node not cave" (premise
  neutral-proof-engineering-framing). A surface the owner cannot name is a
  surface he does not read.

So the notes move here: a small keyed map on DISK under the helm home, written
atomically through pk, rendered on the HOME tab he already opens. The relay,
its adapters and its CLI verbs are untouched — they were never the problem.

SHAPE: {key: {"text": <compat>, "headline": ..., "detail": ..., "goto": ...,
"by": ..., "ts": ...}}. The structured fields are additive and optional, so
legacy text-only rows
project into the same headline-first API without a rewrite. A note is REPLACED
by key (last-writer-wins, exactly like the board cell it replaces), so the file
is a CURRENT-STATE map and not a log.

BOUNDED ON PURPOSE. This is a fleet-writable surface on the owner's front page,
so both axes are capped and both refuse LOUDLY rather than trimming: at most
MAX_KEYS distinct keys (a new key past the cap names the oldest note to retire)
and at most MAX_TEXT characters of text. An unbounded accumulator on a surface
nobody prunes is how a status board becomes a wall the owner stops reading.
"""
import json
import os
import re
import sys
import unicodedata
import urllib.parse

from . import eventledger, home, pk

MAX_KEYS = 64          # distinct notes; a new key past this REFUSES
MAX_TEXT = 2000        # characters across one note's headline + detail
MAX_HEADLINE = 90      # owner surface: one glanceable line, warn rather than trim
MAX_KEY_CHARS = 64
GOTO_TABS = ("ledger", "chat", "roster", "board")

# THE FRESHNESS BOUNDARY, owned HERE because two surfaces read it and a number
# spelled twice is a number that drifts. The owner asked twice on 2026-08-04:
# "the fleet notes still show the older notes instead of latest?" — measured
# that night, ONE note was 2 minutes old and the other five were 5 to 8 DAYS
# old, all rendered as equal rows under a header reading "6 left for you". The
# ordering was already newest-first and each row already showed its age; what
# was missing is that NOTHING SEPARATED them, so a live note sat in a list whose
# bulk was a week stale and the count overstated what actually wanted attention.
#
# A note past this window is STALE, never DELETED. Staleness is a display tier,
# and every surface that dims a note must keep it reachable — the half-arc law:
# the arc that puts a note on the owner's screen owes the arc that takes it off
# WITHOUT destroying it. `helm note rm` remains the only thing that removes.
FRESH_WINDOW_S = 48 * 3600     # 48h: a note older than a working day-and-a-half

# A note key is an IDENTIFIER, validated at THE one ingestion seam (the same
# reflex as home._SEAT_NAME_RE): the key is rendered into the owner's home tab
# and printed to terminals, so an ESC/bidi payload must never become one.
_KEY_RE = re.compile(r"\A[A-Za-z0-9._-]{1,%d}\Z" % MAX_KEY_CHARS)


def path():
    """The notes file. Beside the other durable _global state (the integration
    board, the owner-ask ledger) — never /dev/shm, which is the whole point."""
    return os.environ.get("HELM_FLEET_NOTES") \
        or os.path.join(home.global_dir(), "fleet-notes.json")


def _clean_text(text):
    """NFC, CRLF folded to LF, and REFUSE any remaining control/format/separator
    character. Newline and tab are legitimate in a note (these are paragraphs an
    agent writes for a human); an ESC or a bidi override is not, and refusing at
    the seam beats laundering at every render site forever."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("note text must be a non-empty string")
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n")
                                 .replace("\r", "\n")).strip()
    for ch in text:
        if ch in ("\n", "\t"):
            continue
        if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp"):
            raise ValueError(
                "note text may not contain control or bidi characters "
                "(found %s)" % ch.encode("unicode_escape").decode("ascii"))
    if len(text) > MAX_TEXT:
        raise ValueError("note text is %d characters — the cap is %d. Say the "
                         "headline here and point at the detail"
                         % (len(text), MAX_TEXT))
    return text


def _clean_goto(raw):
    """A pointer is either one cockpit destination or an absolute HTTP(S) URL.

    Owner-surface law `headlines-click-to-detail` (owner law, 2026-07-22 +
    2026-08-04): the headline tells the owner WHAT; the pointer says WHERE to
    act. Validate here because a hand-planted `javascript:` href stays dangerous
    no matter how perfectly HTML-escaped it is."""
    if raw is None:
        return None
    value = raw.strip() if isinstance(raw, str) else ""
    if not value or any(c.isspace() or unicodedata.category(c) in
                        ("Cc", "Cf", "Zl", "Zp") for c in value):
        raise ValueError("note goto must be %s or an absolute http(s) URL"
                         % "|".join(GOTO_TABS))
    if value in GOTO_TABS:
        return value
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("note goto must be %s or an absolute http(s) URL"
                         % "|".join(GOTO_TABS))
    return value


def _headline_warning(text):
    rendered = " ".join(text.splitlines()).strip()
    if len(rendered) > MAX_HEADLINE:
        return ("headline renders as %d characters; owner-facing headlines should "
                "stay at or below %d characters and put the rest in --detail"
                % (len(rendered), MAX_HEADLINE))
    return None


def _clean_key(raw):
    """A key is a NAME. Anything that is not a string refuses here rather than
    AttributeError-ing out of a library call, and the refusal quotes what it was
    handed as printable ASCII — a message about a hostile key must not carry the
    hostile key (home._safe_name's rule)."""
    key = raw.strip() if isinstance(raw, str) else ""
    if not _KEY_RE.match(key):
        raise ValueError(
            "note key is not a legitimate key: '%s' (a key is [A-Za-z0-9._-], "
            "at most %d characters — like reboot-recovery)"
            % (str(raw)[:80].encode("unicode_escape").decode("ascii"),
               MAX_KEY_CHARS))
    return key


def _actor():
    """Who wrote it. chat.whoname() is helm's ONE seat-identity resolver (the
    validated HELM_CHAT_NAME seam, then the roster seat, then the auto-name
    floor) — a note says who left it in the same name the room knows."""
    try:
        from . import chat
        return chat.whoname()
    except Exception:
        return "agent"


def _launder(s):
    """Strip C0/C1 controls (newline and tab kept — a note is prose), Unicode
    format chars including the bidi overrides, and line/paragraph separators.

    DEFENCE IN DEPTH, not the guard. `set_note` REFUSES such a payload at the
    write seam, so a note written through helm never needs this. What needs it
    is a row that entered OUTSIDE the seam: this file is plain JSON on disk and
    a hand-edit is legal (the file is the source of truth, not a projection).
    Both readers are sinks that a control character can reshape — the owner's
    home page and a terminal running `helm note list` — so the render path
    launders too. Same law and same character classes as chat._dsan."""
    if not isinstance(s, str):
        return s
    return "".join(c for c in s if c in ("\n", "\t")
                   or unicodedata.category(c) not in ("Cc", "Cf", "Zl", "Zp"))


def _valid(row):
    return isinstance(row, dict) and isinstance(row.get("text"), str)


def read(notes_path=None):
    """The current notes map. FAIL-OPEN AND TOTAL: a missing file, a corrupt
    file, a file that is a JSON list, a row that is not a note — all read as
    fewer notes, never a raise. This is a read on the owner's front page and on
    every cockpit poll; it may not be able to take the page down."""
    raw = pk.read_json(notes_path or path(), default=None)
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items()
            if isinstance(k, str) and _KEY_RE.match(k) and _valid(v)}


def age_s(ts, now=None):
    """Seconds since this note was written, or None when the stamp will not
    parse. None is NOT zero and NOT infinity: an unreadable stamp means we do
    not know the age, and every consumer must decide that case out loud rather
    than inherit a number nobody measured."""
    import calendar
    import time
    try:
        wrote = calendar.timegm(time.strptime(str(ts), "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None
    return max(0, int((time.time() if now is None else now) - wrote))


def is_stale(ts, now=None):
    """Past the freshness window — a DISPLAY TIER, never a deletion.

    An UNREADABLE stamp is NOT stale. The failure to prefer is the one that
    keeps a note in front of the owner: dimming a note we could not date would
    hide it on the strength of a number we never read, and this whole lane
    exists because the owner could not see a live note."""
    secs = age_s(ts, now)
    return secs is not None and secs > FRESH_WINDOW_S


def _project(key, row, now=None):
    """Add the owner-facing shape without rewriting legacy rows.

    Existing notes are prose blobs. `headlines-click-to-detail` (owner law,
    2026-07-22 + 2026-08-04) means a long or multiline legacy body must improve
    on its NEXT read: derive one bounded headline and collapse the untouched
    original into detail. New structured rows carry detail/goto explicitly and
    keep their full warned-not-trimmed headline."""
    text = _launder(row.get("text", ""))
    stored_headline = row.get("headline")
    explicit = isinstance(stored_headline, str) or "detail" in row or "goto" in row
    detail = _launder(row.get("detail")) if isinstance(row.get("detail"), str) else ""
    if explicit:
        headline = " ".join(_launder(stored_headline if isinstance(
            stored_headline, str) else text).splitlines()).strip()
    elif "\n" in text or len(text) > MAX_HEADLINE:
        first = next((line.strip() for line in text.splitlines() if line.strip()), text)
        headline = (first[:MAX_HEADLINE - 1] + "…"
                    if len(first) > MAX_HEADLINE else first)
        detail = text
    else:
        headline = text
    try:
        goto = _clean_goto(row.get("goto"))
    except ValueError:
        goto = None                    # hand-edited unsafe pointer: fail closed
    ts = row.get("ts") or ""
    # ONE CLOCK READ, THREADED. age_s and is_stale each fall through to their own
    # time.time() when now is None, so calling both left a row whose age and
    # staleness were measured microseconds apart — enough to straddle the
    # boundary and report age 47h59m59s alongside stale=True (codex, review of
    # the first #234 tip). A row must describe ONE instant.
    secs = age_s(ts, now)
    retired = bool(row.get("retired"))
    return {"key": key, "text": text, "headline": headline,
            "detail": detail, "goto": goto,
            "by": _launder(row.get("by")) or "?", "ts": ts,
            "age_s": secs, "retired": retired,
            # RETIRED LEAVES THE FRESH SECTION AT ANY AGE. Retirement is the
            # author declaring a note done; staleness is the clock declaring it
            # old. Both land in the same fold and neither destroys anything.
            "stale": retired or (secs is not None and secs > FRESH_WINDOW_S)}


def rows(notes_path=None, now=None):
    """Every note as a list, NEWEST FIRST — the order the question "what
    happened overnight" is actually asked in. Undated rows sort last, then by
    key, so the order is total and two reads never disagree.

    Each row carries `stale`, computed from ONE boundary (FRESH_WINDOW_S) so the
    web card and the CLI cannot disagree about which notes are old.

    AN UNDATABLE NOTE SORTS FIRST, NOT LAST, and that is a correction. It used to
    sort last (an empty stamp is the smallest string) while being classified
    FRESH — so partitioning lifted it above genuinely older notes and the feed
    order and the rendered order disagreed (codex, review of the first #234 tip).
    Sorting it first makes the two agree, and it is the honest place for it: a
    note we cannot even date is the one whose age nobody can vouch for, and this
    lane exists because notes were disappearing from the owner's attention.
    The invariant that follows, and which a test now pins: SPLITTING NEVER
    REORDERS — concatenating (fresh + older) reproduces rows() exactly."""
    out = [_project(k, v, now) for k, v in read(notes_path).items()]
    # THE FEED ORDER IS THE TIER ORDER, so splitting cannot reorder BY
    # CONSTRUCTION rather than by coincidence. Sorting by recency alone was
    # order-preserving only while "stale" meant "old", and it broke the moment
    # RETIREMENT arrived: retiring the NEWEST note left it at the top of the
    # feed and in the bottom section (codex, r2 review). My no-reorder test did
    # not retire anything, so it passed while the property was false — the third
    # time in this lane a test was bound to the cases already in my head.
    out.sort(key=lambda r: (r["ts"], r["key"]), reverse=True)
    # STABLE second pass: fresh above stale, and undatable first within its
    # tier. Python's sort is stable, so the recency order above survives inside
    # each group and split_fresh returns two CONTIGUOUS runs of this list.
    out.sort(key=lambda r: (1 if r["stale"] else 0,
                            0 if r["age_s"] is None else 1))
    return out


def split_fresh(notes):
    """(fresh, older) preserving order within each — the shape every surface
    needs and none should re-derive. NOTHING is dropped: len(fresh)+len(older)
    always equals len(notes), which a test pins, because the one outcome this
    lane must never produce is a note that stopped being reachable."""
    fresh = [n for n in notes if not n.get("stale")]
    older = [n for n in notes if n.get("stale")]
    return fresh, older


def _update(mutate, notes_path=None):
    """Apply mutate(notes_dict) under the exclusive sibling lock — read AND
    write inside ONE lock, the board.update contract. Two agents leaving notes
    in the same second is the ordinary case here, not the exotic one, and an
    API that hands back the parsed map for the caller to write later re-opens
    the lost update with more steps. A mutate that raises writes nothing."""
    p = notes_path or path()
    with eventledger.locked(p) as held:
        if not held:
            return None, ("fleet notes are locked by another writer (or the "
                          "lock could not be taken) — nothing written")
        notes = read(p)
        out = mutate(notes)
        pk.write_json(p, notes)
        return out, None


def _clean_ts(ts):
    """An explicit stamp must BE a stamp in helm's one time format, or the age
    column silently reads '?' forever. Empty means now."""
    import time
    if not ts:
        return pk.now_ts()
    try:
        time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        raise ValueError("ts must be YYYY-MM-DDThh:mm:ssZ, got " + repr(ts))
    return ts


def set_note(key, text, by=None, ts=None, notes_path=None, *, detail=None,
             goto=None, idempotent=False):
    """Write one note, REPLACING any note under that key -> (row, err).

    `text` remains the compatibility field and is now the HEADLINE. Optional
    detail and goto make the owner's surface compositional: one glanceable line,
    native click-to-detail, then an explicit place to act. This is the
    `headlines-click-to-detail` owner-surface law (owner law, 2026-07-22 +
    2026-08-04), enforced at the one write seam rather than improvised by the
    browser.

    The key cap applies to NEW keys only: replacing an existing note is how the
    fleet keeps its own status current, and refusing that at the cap would
    freeze the board in whatever state it hit 64 in. `idempotent=True` compares
    the structured content and skips an exact match INSIDE the notes lock; this
    is for derived projections whose retry must repair a mismatch without
    refreshing an already-correct timestamp.

    `by`/`ts` exist for PROVENANCE-PRESERVING IMPORT — the migration that
    carried the three overnight notes off the retired tmpfs demo board had to
    keep the agent who wrote each one and the moment it was written, and
    stamping them all as "me, now" would have been the migration quietly
    forging its own input. The CLI never passes them; a live writer is always
    the current seat at the current moment."""
    try:
        key, text, ts = _clean_key(key), _clean_text(text), _clean_ts(ts)
        detail = _clean_text(detail) if detail is not None else None
        goto = _clean_goto(goto)
        total = len(text) + (len(detail) if detail else 0)
        if total > MAX_TEXT:
            raise ValueError("note headline + detail are %d characters — the cap "
                             "is %d" % (total, MAX_TEXT))
    except ValueError as e:
        return None, str(e)
    # `by` is DERIVED (chat.whoname's validated seam) rather than typed, so it
    # is laundered and bounded instead of refused — refusing a name the caller
    # did not choose would drop a legitimate note.
    row = {"text": text, "headline": text,
           "by": _launder(str(by or _actor()))[:80] or "agent", "ts": ts}
    if detail is not None:
        row["detail"] = detail
    if goto is not None:
        row["goto"] = goto
    warning = _headline_warning(text)

    def _mutate(notes):
        current = notes.get(key)
        if idempotent and isinstance(current, dict) and all(
                current.get(field) == row.get(field)
                for field in ("text", "headline", "detail", "goto")):
            return dict(current, key=key, unchanged=True)
        if key not in notes and len(notes) >= MAX_KEYS:
            raise ValueError(
                "fleet notes hold %d keys, the cap — refusing to add '%s'. "
                "Retire one first (helm note retire <key>, or rm to DELETE); "
                "the oldest is '%s'"
                % (len(notes), key, min(notes, key=lambda k: (
                    notes[k].get("ts") or "", k))))
        notes[key] = dict(row)
        return dict(row, key=key)

    try:
        out, err = _update(_mutate, notes_path)
    except ValueError as e:
        return None, str(e)
    if err:
        return None, err
    if warning:
        out["warning"] = warning       # transient: advice is not stored as truth
    if not out.get("unchanged"):
        pk.event("note-set", key, text)
    return out, None


def retire(key, retired=True, notes_path=None):
    """Stop a note competing for the owner's attention WITHOUT destroying it.

    THE HALF-ARC LAW, applied to the note's LIFECYCLE rather than its display.
    This lane already gives the entry arc (a note appears) an exit arc that
    preserves it (a stale note folds, still reachable) — and then claimed `rm`
    served retirement. It does not: `rm` DELETES, and a lane whose whole thesis
    is "dimmed, never dropped" cannot offer deletion as its retirement path
    (codex, review of the first #234 tip; the defect was my CLAIM, and the
    honest repair is to build the thing I said existed).

    A retired note leaves the fresh section regardless of age and lives in the
    same fold as older ones, so `helm note retire` is undoable by
    `helm note restore` and loses nothing. Returns (ok, err); an absent key is
    an honest False, never a raise.
    """
    try:
        key = _clean_key(key)
    except ValueError as e:
        return False, str(e)
    if key not in read(notes_path):
        return False, None

    def _mutate(notes):
        row = notes.get(key)
        if not isinstance(row, dict):
            return False
        if retired:
            row["retired"] = True
        else:
            row.pop("retired", None)
        return True

    ok, err = _update(_mutate, notes_path)
    return bool(ok) and not err, err


def remove(key, notes_path=None):
    """DELETE one note -> (ok, err). This is destruction and the name says so:
    the row is gone from disk and no verb brings it back. For "stop showing me
    this, but keep it", the verb is `retire` — this docstring used to say
    "Retire one note" over a dict.pop, which is exactly the false seam a
    reviewer caught in the lane that added the fold.

    Absent is an honest False, not an error: the caller asked for a key that is
    not there and deserves to be told."""
    try:
        key = _clean_key(key)
    except ValueError as e:
        return False, str(e)
    if key not in read(notes_path):
        # Nothing to remove, so take no lock and write no file — otherwise
        # `helm note rm typo` CREATES an empty store as a side effect of a
        # command that did nothing. The unlocked peek is a benign race: a
        # writer adding this key a millisecond later does not make "there was
        # no note under that key when I looked" false, and the real removal
        # below still happens under the lock.
        return False, None

    def _mutate(notes):
        return notes.pop(key, None) is not None

    removed, err = _update(_mutate, notes_path)
    if err:
        return False, err
    if removed:
        pk.event("note-rm", key, "DELETED (destructive; retire is the undoable one)")
    return bool(removed), None


# ---------------------------------------------------------------------------
# the verb
# ---------------------------------------------------------------------------

_USAGE = """usage: helm note set <key> <headline...> [--detail <body...>] [--goto <tab-or-url>]
       helm note list [--json]
       helm note retire <key>     stop it competing for attention, KEEP it
       helm note restore <key>    undo a retire
       helm note rm <key>         DELETE it (destructive; retire is the undoable one)
  Fleet notes — what the fleet leaves the OWNER, on the cockpit's home tab.
  Headlines stay one line and point to collapsed detail plus ledger|chat|roster|
  board or an http(s) URL. Headlines over %d characters WARN; at most %d keys
  and %d characters across headline + detail, both hard caps refused loudly.
  HELM_FLEET_NOTES overrides the path.
""" % (MAX_HEADLINE, MAX_KEYS, MAX_TEXT)


def _age(ts):
    """A rough human age off the ISO stamp, or the stamp itself when it will
    not parse — the CLI never lies about a time it could not read."""
    import calendar
    import time
    try:
        secs = time.time() - calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return ts or "?"
    if secs < 3600:
        return "%dm ago" % max(0, secs // 60)
    if secs < 86400:
        return "%dh ago" % (secs // 3600)
    return "%dd ago" % (secs // 86400)


def _set_args(rest):
    """Parse the structured tail without making quoted headlines mandatory.

    Tokens before the first flag remain the headline, so the original
    `note set key several words` shape keeps working. Detail is prose and joins;
    goto is one destination. Unknown/duplicate flags refuse instead of quietly
    becoming owner-facing text."""
    if len(rest) < 2:
        raise ValueError("set needs a key and headline")
    fields, current = {"headline": []}, "headline"
    for token in rest[1:]:
        if token in ("--detail", "--goto"):
            current = token[2:]
            if current in fields:
                raise ValueError("%s may appear only once" % token)
            fields[current] = []
        elif token.startswith("--"):
            raise ValueError("unknown note set option %s" % token)
        else:
            fields[current].append(token)
    headline = " ".join(fields["headline"]).strip()
    if not headline:
        raise ValueError("set needs a non-empty headline before --detail/--goto")
    detail = " ".join(fields.get("detail", [])).strip() \
        if "detail" in fields else None
    if "detail" in fields and not detail:
        raise ValueError("--detail needs a body")
    goto = fields.get("goto")
    if goto is not None and len(goto) != 1:
        raise ValueError("--goto needs exactly one tab or URL")
    return rest[0], headline, detail, goto[0] if goto else None


def _advise_owner(text):
    """OWNER-BOUND ADVISORY. This verb writes text the owner reads, so it runs
    the clarity die in owner mode and prints what it finds. Advisory by law:
    it cannot refuse the write and cannot change the exit code, and every
    failure path is silent (see helm.clarity.advise). Silence it with
    HELM_CLARITY_ADVISE_OFF=note."""
    try:
        from .clarity import advise
        advise(text, "note")
    except Exception:                    # noqa: BLE001 — fail-open by law
        pass


def cmd_note(args):
    """note set|list|rm — the fleet's durable notes to the owner."""
    args = list(args or [])
    verb = args[0] if args else ""
    if not verb or verb in ("-h", "--help", "help"):
        print(_USAGE)
        return 0 if verb else 2
    if verb == "list":
        found = rows()
        if "--json" in args:
            print(json.dumps(found, indent=2, ensure_ascii=False))
            return 0
        if not found:
            print("helm note: no fleet notes yet — `helm note set <key> <text>`")
            return 0
        # SAME BOUNDARY AS THE CARD. The web collapses stale notes behind a
        # fold; the CLI cannot fold, so it RULES them off instead — but both
        # read FRESH_WINDOW_S, so the two surfaces can never disagree about
        # which notes are old. Every note still prints: this is a divider, not
        # a filter, and `--json` is untouched for anything parsing it.
        fresh, older = split_fresh(found)
        for r in fresh:
            print("  %-24s %s  — %s, %s" % (r["key"], r["headline"][:80],
                                            r["by"], _age(r["ts"])))
        if older:
            print("  — %d older than %dh (kept, not deleted) —"
                  % (len(older), FRESH_WINDOW_S // 3600))
            for r in older:
                print("  %-24s %s  — %s, %s" % (r["key"], r["headline"][:80],
                                                r["by"], _age(r["ts"])))
        print("  (%d/%d keys · %s)" % (len(found), MAX_KEYS, path()))
        return 0
    if verb == "set":
        try:
            key, headline, detail, goto = _set_args(args[1:])
        except ValueError as e:
            print("helm note: " + str(e), file=sys.stderr)
            print(_USAGE, file=sys.stderr)
            return 2
        row, err = set_note(key, headline, detail=detail, goto=goto)
        if err:
            print("helm note: " + err, file=sys.stderr)
            return 1
        if row.get("warning"):
            print("helm note: WARNING: " + row["warning"], file=sys.stderr)
        print("helm note: %s set by %s" % (row["key"], row["by"]))
        _advise_owner(" ".join(x for x in (headline, detail) if x))
        return 0
    if verb in ("retire", "restore"):
        rest = args[1:]
        if len(rest) != 1:
            print(_USAGE, file=sys.stderr)
            return 2
        want = verb == "retire"
        ok, err = retire(rest[0], retired=want)
        if err:
            print("helm note: " + err, file=sys.stderr)
            return 1
        if not ok:
            print("helm note: no note under '%s'" % rest[0], file=sys.stderr)
            return 1
        print("helm note: %s %s — %s"
              % (rest[0], "retired" if want else "restored",
                 "folded with the older notes, still on disk; "
                 "`helm note restore %s` undoes it" % rest[0] if want
                 else "back in the current section"))
        return 0
    if verb == "rm":
        rest = args[1:]
        if len(rest) != 1:
            print(_USAGE, file=sys.stderr)
            return 2
        ok, err = remove(rest[0])
        if err:
            print("helm note: " + err, file=sys.stderr)
            return 1
        if not ok:
            print("helm note: no note under '%s'" % rest[0], file=sys.stderr)
            return 1
        print("helm note: %s DELETED — gone from disk. "
              "`helm note retire <key>` is the undoable one" % rest[0])
        return 0
    from .cli import suggest
    # the unknown verb is unvalidated argv going STRAIGHT to a terminal, so it
    # is echoed as printable ASCII — a refusal must not run the payload it is
    # refusing (the same rule home._safe_name applies to a hostile seat name)
    safe = str(verb)[:40].encode("unicode_escape").decode("ascii")
    print("helm note: unknown verb '%s'%s" % (
        safe, suggest(verb, ("set", "list", "rm"))), file=sys.stderr)
    print(_USAGE, file=sys.stderr)
    return 2
