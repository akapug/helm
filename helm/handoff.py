#!/usr/bin/env python3
"""helm handoff + now — compaction continuity: the wheel survives the window.

Two legs on one PreCompact trigger (the handoff-now card):

  handoff (AUTHORED, primary) — the handoff contract. `write` turns stdin
  DONE/REMAINING/NEXT prose into a typed journal entry on the project shelf
  (~/.helm/<project>/journal/) — greppable, cv-searchable, ship/pull-able.
  `check` asks ONE question: does a handoff artifact for THIS session exist
  (a journal entry carrying the session id, or a repo HANDOFF_NEXT_SESSION.md)
  newer than a FLOOR? Hook-wired on PreCompact + SessionEnd it NAGS in a
  few lines when unmet — a nag, never a capture: the agent authors audited
  prose, helm never invents a summary. `recover <sid>` re-reads the span a
  compaction discarded by wrapping the ONE recall index (`cv show <sid>
  --pre-compaction`, architecture law 4 — record how to query, never a
  second index).

  THE FLOOR IS PER-EVENT, and the session is the wrong unit for a compaction
  (bug class `compaction-floors-on-session-start`). Measured live 2026-07-28:
  a PreCompact hook reported "contract satisfied — 2026-07-27-handoff-….md
  (37h old)" and therefore wrote NO handoff for the compaction happening right
  then. Root cause: the floor was SESSION start, and CC 2.1.217+ keeps ONE
  session id and ONE transcript across a compaction (verified over 461
  compact_boundary records in this estate — every one of them carries the same
  sessionId as its transcript). So session start never moves, the gate widens
  monotonically as a session ages, and by hour 37 anything ever written passes.
  That is backwards: the older the session, the MORE it has discarded and the
  less its birth-era handoff describes. `compaction_floor` scopes the question
  to the compaction EPISODE instead — see it for the three legs.

  now (AUTOMATIC safety net) — `capture` snapshots session id + project +
  git branch/status/changed paths + this session's recorded edits and reflex
  counters into _global/now.md: NEWEST-FIRST, 40-line cap. `show` prints it
  ONLY while <48h fresh (a stale now.md actively misleads — the gate is
  load-bearing, not polish), shaped for SessionStart additionalContext;
  legacy ~/.remember/now.md is honored as a read fallback until retired.

Laws:
  * SESSION-keyed from the payload's session_id, never pane (record.py law).
  * Hook mode is FAIL-OPEN TOTAL: capture never blocks a compaction — git
    probes carry a 3s timeout, every exception is swallowed, rc 0 always,
    SILENT when the contract is satisfied (salience law).
  * now.md is DERIVED telemetry (rebuildable, never ships, no receipt); the
    handoff entry is AUTHORED (journal shelf, mutation receipt, ships).
"""
import fnmatch
import json
import os
import re
import sys
import time

from . import home, pk, vcs

FRESH_H = 48          # the STANDING freshness constant: show's gate AND check's
EVENT_FRESH_H = 2     # …and the EVENT window: how recently a handoff must have
                      # been authored to count as authored FOR this compaction
                      # (HELM_HANDOFF_EVENT_FRESH_H). Sized from this estate's
                      # own compaction spacing: consecutive compactions of one
                      # session sit 6–16h apart (session f0ad7476, six of them
                      # 07-22..07-25), so 2h separates "written for this event"
                      # from "left over from the previous window" without being
                      # a hair trigger.
NOW_LINES = 40        # unknown-session-start window (a stale artifact misleads)
GIT_TIMEOUT = 3
TAIL_BYTES = 512 * 1024   # bounded transcript tail (autocompact's law)
REPO_FILE = "HANDOFF_NEXT_SESSION.md"
# argv-safety (transcripts.py law): a sid riding subprocess argv must look
# like an id — never like a flag.
_SAFE_SID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
# a section heading counts only as `# DONE ...` / `**DONE**` / `DONE: ...` /
# `DONE (this window)` — bare prose starting with the word never false-matches.
# The PARENTHETICAL rung was added 2026-07-29 after this integrator's own
# handoff wrote `DONE (this window, post-compaction)` and parsed as NOTHING: a
# qualifier on a heading is the most natural thing an author writes, and the
# pattern that rejected it made the contract silently unmeetable.
# A BARE keyword line (`REMAINING` alone, bullets under it) counts too, but
# only UNINDENTED — that is what separates a heading from a status stamp
# indented under a task (`  DONE`). Both widenings come from the same measured
# failure: a handoff written the way an author actually writes one parsed as
# nothing at all, and the surface that should have caught it said "satisfied".
# The WORD-RUN rung (measured 2026-08-02) is the parenthetical rung's class a
# third time: `DONE this window — THREE lanes` parsed as NOTHING, because the
# qualifier rung only covered a qualifier IN PARENTHESES and bare words are at
# least as natural to write. A bounded run of qualifier words may now sit
# between the keyword and the separator — bounded to 40 chars, and only WITH a
# separator to its right, so a prose sentence that merely starts with the word
# still cannot promote its own clause into the contract.
_SECTION = re.compile(
    r"^(?P<indent>[ \t]*)(?P<hash>#+\s*)?(?P<bold>\*\*)?"
    r"(?P<key>DONE|REMAINING|NEXT)\b\**"
    r"(?P<qual>\s*\([^)]*\)|\s[\w\s,()-]{0,40}?(?=[:\-—/]))?"
    r"\s*(?P<tail>[:\-—/].*)?$", re.I)

# A line carrying an EXPLICIT heading marker (`#` or `**`) is a heading, and
# whatever follows the keyword is a qualifier. Restricting the qualifier to an
# immediate parenthetical rejected `## REMAINING / IN FLIGHT (updated …)` and
# `## NEXT STEPS` — both natural, both live on the shelf. Measured 2026-07-29:
# a real, current, richly-written handoff read as HOLLOW and the PreCompact nag
# told its author "no handoff artifact — the next window starts blind" about a
# file that existed and carried the content. Widening the pattern for
# explicitly-marked lines only keeps the strictness where it earns its keep:
# an UNMARKED line still has to be the bare keyword or carry `:`/`-`/`—`/`(`,
# so prose never false-matches.
_MARKED = re.compile(
    r"^(?P<indent>[ \t]*)(?P<hash>#+\s*)?(?P<bold>\*\*)?"
    r"(?P<key>DONE|REMAINING|NEXT)\b(?P<rest>.*)$", re.I)

# Frontmatter, stripped before any section parse. Its `done:`/`remaining:`/
# `next:` keys match _SECTION exactly, so parsing an entry WITH its frontmatter
# makes a hollow handoff re-read as three populated sections whose content is
# the next key in the block — measured, and the reason `hollow()` reads a body.
_FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.S)


def now_path():
    return os.path.join(home.global_dir(), "now.md")


def legacy_now_path():
    """~/.remember/now.md — the convention this snapshot replaces; read
    fallback until retired. HELM_REMEMBER_DIR overrides (tests)."""
    d = home.env("REMEMBER_DIR") or os.path.join(os.path.expanduser("~"), ".remember")
    return os.path.join(os.path.expanduser(d), "now.md")


def _git(cwd, *args):
    """git -C <cwd> <args> -> stripped stdout; None on ANY trouble (no git,
    nonzero, timeout) — a probe must never block a compaction. The spawn lives
    behind the VCS seam (helm/vcs.py `probe`); the 3s budget and the cwd-or-`.`
    floor stay here because they are handoff's, not the seam's."""
    return vcs.backend(cwd or ".").probe(cwd or ".", *args, timeout=GIT_TIMEOUT)


def _hook(raw):
    """Hook event JSON -> dict, {} on garbage — unknown keys tolerated;
    PreCompact / SessionEnd / SessionStart payloads all parse the same."""
    try:
        d = json.loads(raw or "")
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


def _fresh(path, hours=FRESH_H):
    """Age in seconds while inside the freshness window, else None."""
    try:
        age = time.time() - os.stat(path).st_mtime
    except OSError:
        return None
    return age if age < hours * 3600 else None


def _age(secs):
    secs = int(secs)
    if secs < 120:
        return "%ds" % secs
    if secs < 7200:
        return "%dm" % (secs // 60)
    return "%dh" % (secs // 3600)


_TS = re.compile(r'"timestamp"\s*:\s*"(\d{4})-(\d{2})-(\d{2})'
                 r'[T ](\d{2}):(\d{2}):(\d{2})')
# CC writes the compaction seam as one system record; `subtype` is the field
# that names it (verified against 461 real boundaries, CC 2.1.183 → 2.1.220).
_BOUNDARY = re.compile(r'"subtype"\s*:\s*"compact_boundary"')


def _stamp(line):
    """A transcript line's UTC timestamp as an epoch, None when unstamped."""
    m = _TS.search(line)
    if not m:
        return None
    import calendar
    return float(calendar.timegm(tuple(int(x) for x in m.groups()) + (0, 0, 0)))


def _session_start(transcript):
    """Session start epoch from the transcript's first timestamped lines
    (claude's opening lines are unstamped bookkeeping — scan a bounded head);
    file ctime fallback; None when nothing is known."""
    if not transcript:
        return None
    try:
        with open(transcript, encoding="utf-8", errors="replace") as f:
            for i, ln in enumerate(f):
                if i >= 80:
                    break
                at = _stamp(ln)
                if at is not None:
                    return at
        return os.stat(transcript).st_ctime
    except OSError:
        return None


def last_compaction(transcript):
    """Epoch of the newest compact_boundary in this transcript, else None.

    CC does not rotate the file on a compaction, so the boundary record IS the
    only in-band marker of where the previous window ended. Bounded tail read
    (a live transcript reaches hundreds of MB); a boundary older than the tail
    reads as None, which only ever RELAXES the floor back onto its other legs
    — never invents one."""
    if not transcript:
        return None
    try:
        size = os.path.getsize(transcript)
        with open(transcript, "rb") as f:
            if size > TAIL_BYTES:
                f.seek(size - TAIL_BYTES)
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for ln in reversed(lines):
        if _BOUNDARY.search(ln):
            at = _stamp(ln)
            if at is not None:
                return at
    return None


def compaction_floor(start, transcript, now=None):
    """The mtime a handoff must beat to have been written FOR this compaction.

    THREE legs, each closing a hole the others leave open:
      1. session start — the standing floor, unchanged.
      2. the previous compact_boundary — an artifact authored before the LAST
         compaction cannot describe what THIS one discards; that span is
         already gone. Load-bearing for every session that compacts twice, and
         they do: one session id here holds six boundaries.
      3. now − EVENT_FRESH_H — the leg that fixes the live 2026-07-28 miss.
         Legs 1 and 2 are both stuck at the session's birth on its FIRST
         compaction, and a session's birth can be days back, so without a
         bound on the clock a 37h-old handoff satisfies a compaction happening
         now.
    Newest wins: the floor is the tightest of the three."""
    floors = [(now or time.time()) - _event_fresh_h() * 3600]
    for leg in (start, last_compaction(transcript)):
        if leg is not None:
            floors.append(leg)
    return max(floors)


def _event_fresh_h():
    try:
        return float(home.env("HANDOFF_EVENT_FRESH_H", EVENT_FRESH_H))
    except (TypeError, ValueError):
        return EVENT_FRESH_H


def _project(cwd, explicit=None):
    """Explicit --project wins; else the registry's longest-prefix owner of
    cwd (inject's one derivation — never a second resolver)."""
    if explicit:
        return explicit
    from . import inject
    return inject.project_for_cwd(cwd)


# ---------------------------------------------------------------------------
# now — the automatic safety net
# ---------------------------------------------------------------------------

def snapshot(sid, cwd):
    """One session's now-block, <=5 terse lines — a missing source (no git,
    no reflex state) drops its line, never errs."""
    from . import record
    lines = ["## %s sid=%s project=%s"
             % (pk.now_ts(), (sid or "-")[:8], _project(cwd) or "-")]
    branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if branch is None:
        lines.append("   cwd %s  (no git)" % (cwd or "-"))
    else:
        porc = (_git(cwd, "status", "--porcelain") or "").splitlines()
        lines.append("   cwd %s  branch %s  %s" % (
            cwd or "-", branch, ("dirty %d" % len(porc)) if porc else "clean"))
        if porc:
            # split, never a column offset: _git strips stdout, so the FIRST
            # porcelain line loses its leading status space (live-smoke catch)
            lines.append("   changed: " + " ".join(
                l.split(None, 1)[1] for l in porc[:4] if len(l.split(None, 1)) > 1))
    if sid:
        try:
            with open(os.path.join(record.session_dir(sid), "edit-targets.log"),
                      encoding="utf-8") as f:
                seen = []
                for name in reversed(f.read().splitlines()):
                    if name and name not in seen:
                        seen.append(name)
                    if len(seen) == 5:
                        break
            if seen:
                lines.append("   edited: " + " ".join(seen))
        except OSError:
            pass
        c = record.counters(sid)
        if c:
            lines.append("   reflex: passive=%s dirty=%s stuck=%s loop=%s" % (
                c.get("passive-streak", 0), c.get("dirty-streak", 0),
                c.get("stuck-streak", 0), c.get("loop-streak", 0)))
    return lines


def capture(sid, cwd):
    """Prepend one snapshot block to _global/now.md, NEWEST-FIRST, capped at
    NOW_LINES. FAIL-OPEN TOTAL -> path or None: capture never raises and
    never blocks the compaction that triggered it. DERIVED telemetry — no
    event receipt, and ship never carries it."""
    try:
        block = snapshot(sid, cwd)
        path = now_path()
        try:
            with open(path, encoding="utf-8") as f:
                old = f.read().splitlines()
        except OSError:
            old = []
        pk.atomic_write(path, "\n".join(
            (block + ([""] if old else []) + old)[:NOW_LINES]) + "\n")
        return path
    except Exception:
        return None


def show():
    """The freshest snapshot text, or '' — _global/now.md while <48h fresh,
    else the legacy ~/.remember/now.md (read fallback until retired). Stale
    or absent -> EMPTY: a stale now.md actively misleads; silence is the
    truthful output (and SessionStart injects nothing)."""
    for path in (now_path(), legacy_now_path()):
        if _fresh(path) is None:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read().strip()
        except OSError:
            continue
        if text:
            return "helm now (session-continuity snapshots, newest first):\n" + text
    return ""


# ---------------------------------------------------------------------------
# handoff — the authored contract
# ---------------------------------------------------------------------------

def _summaries(text):
    """DONE/REMAINING/NEXT one-liners out of handoff prose: a heading's inline
    remainder, else its first non-empty following line."""
    out = {}
    lines = (text or "").splitlines()
    for i, ln in enumerate(lines):
        marked = _MARKED.match(ln)
        # An explicit `#`/`**` marker makes it a heading whatever trails the
        # keyword; without one, fall back to the strict form so prose cannot
        # promote itself.
        if marked and (marked.group("hash") or marked.group("bold")):
            key, rest = marked.group("key").lower(), marked.group("rest")
            # emphasis markers come OFF FIRST, because the colon can sit inside
            # them: `**REMAINING:** the hook` leaves `:** the hook`, and testing
            # for the colon before stripping `*` published "** the hook" as the
            # summary.
            rest = rest.replace("*", "").strip()
            # ONLY a colon introduces CONTENT on a marked line. `## NEXT STEPS`
            # and `## REMAINING / IN FLIGHT (updated …)` are heading TEXT, and
            # lifting them as the summary published "STEPS" and "IN FLIGHT
            # (updated …)" as this session's next step — a frontmatter field
            # that is present, wrong, and reads as populated. Anything not
            # introduced by a colon falls through to the first content line.
            rest = rest[1:].strip() if rest.startswith(":") else ""
        else:
            m = _SECTION.match(ln)
            if not m or not (m.group("qual") or m.group("tail") is not None
                             or not m.group("indent")):
                continue
            key = m.group("key").lower()
            rest = (m.group("tail") or "")[1:].strip().lstrip("*").strip()
        if key in out:
            continue
        if not rest:
            rest = next((l.strip().lstrip("-* ") for l in lines[i + 1:]
                         if l.strip()), "")
        out[key] = re.sub(r"\s+", " ", rest)
    return out


def _own_seat():
    """This process's DECLARED seat name ($HELM_CHAT_NAME through the one
    validated seam), or "" — the authorship stamp, and never the auto-name
    floor. `seats.derive_seat` falls through to a project+family auto-name, so
    five claude seats all bottom out at the bare family name `claude`: a
    floor-derived stamp would let them claim each other's handoffs, which is
    the exact failure this key exists to close. An undeclared writer stamps
    NOTHING, which reads honestly as unproven rather than as a false match."""
    try:
        from . import seats
        return seats.own_name() or ""
    except Exception:                 # noqa: BLE001 — an unstampable author is
        return ""                     # unproven authorship, never a traceback


def write_entry(text, project, sid):
    """stdin prose -> the typed journal entry on the project shelf
    (<date>-handoff-<sid8>.md; a same-day same-session re-write lands on the
    same path — the newest handoff wins). Frontmatter carries the one-line
    DONE/REMAINING/NEXT summaries, the session id, and the AUTHOR SEAT, so the
    entry is greppable, searchable, ATTRIBUTABLE, and ships with the authored
    chain.
    -> (path, ()) — or (None, missing-section-names) with NOTHING written when
    a required section parses empty. The old shape warned on stderr and wrote
    anyway, and the empty done: it left behind is exactly what `handoff check`
    then reported as an entry that exists (measured 2026-08-02, the word-run
    rung): the dropped section is the state the next window resumes on.

    The seat key is what makes the shelf safe to share. It is per-PROJECT and
    every seat on the project writes to it, so an entry that cannot name its
    author is an entry another seat can be handed as its own — measured
    2026-07-31, `attribute_entry`."""
    s = _summaries(text)
    missing = [k for k in ("done", "remaining", "next") if not s.get(k)]
    if missing:
        # A parser that cannot see a section must not emit a confident
        # artifact: an entry carrying an empty done: reads to `handoff check`
        # as a discharged contract, which is worse than writing nothing.
        return None, missing
    ts = pk.now_ts()
    tag = "%s-handoff-%s" % (ts[:10], (sid or "session")[:8])
    path = os.path.join(home.project_dir(project), "journal", tag + ".md")
    head = next((s[k] for k in ("next", "done", "remaining") if s.get(k)), "") \
        or next((l.strip() for l in text.splitlines() if l.strip()), "")
    body = [
        "---",
        "name: " + tag,
        'description: "handoff: ' + head.replace('"', "'")[:150] + '"',
        "metadata:",
        "  type: handoff",
        "  session_id: " + (sid or ""),
        "  seat: " + _own_seat(),
        "  project: " + project,
        "  ts: " + ts,
        "  done: " + s.get("done", ""),
        "  remaining: " + s.get("remaining", ""),
        "  next: " + s.get("next", ""),
        "---", "", text.rstrip(), ""]
    pk.atomic_write(path, "\n".join(body))
    pk.event("handoff.write", sid or project, project + " — " + head)
    return path, []


def _misread(text, missing):
    """The line a refusal QUOTES: the first line that NAMES a missing keyword
    without parsing as its heading — the author's near-miss — else the first
    non-empty line, so the author reads their own prose back, never a regex."""
    lead = re.compile(r"[#*\s]*(?:%s)\b" % "|".join(missing), re.I)
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    return next((l for l in lines if lead.match(l)), lines[0] if lines else "")


_ENTRY_KEYS = {"next": "", "done": "", "remaining": "", "session_id": "",
               "seat": "", "ts": ""}

NO_SHELF = "no-shelf"            # no journal directory / no entries at all
NONE_FRESH = "none-fresh"        # entries exist, none written for this compaction
FOREIGN_ONLY = "foreign-only"    # fresh entries exist and name OTHER seats
UNATTRIBUTED = "unattributed"    # fresh entries exist, authorship unprovable
UNREADABLE = "unreadable"        # an entry could not be read AT ALL — say so

BY_SID = "session"               # how a returned entry was proven this caller's
BY_SEAT = "seat"


def attribute_entry(project, sid=None, floor=None):
    """The freshest fresh handoff PROVABLY authored by THIS process
    -> (path, meta, BY_SID|BY_SEAT), else (None, None, <reason>).

    Ordered by MTIME, not by name: the filename carries a date, so name order
    cannot separate two entries written the same day.

    WHY PROOF IS REQUIRED, and this reverses a deliberate earlier choice. `sid`
    used to be "a PREFERENCE, never a requirement" — CC 2.1.217+ carries one
    session id across a compaction, but older CC forked a new id at the
    boundary, so a reader that DEMANDED the match would find nothing exactly
    when it matters most. Sound reasoning, and its fallback (newest entry above
    the floor) is correct on a ONE-SEAT shelf. helm's shelf is per-PROJECT and
    every seat on the project writes to it, so the fallback is a LOTTERY among
    whichever seats compacted inside the same floor window — and `resume_text`
    printed "Your own handoff … says NEXT: …  Do not wait for a human" over
    whatever it drew. Measured 2026-07-31: a seat woke to another seat's NEXT
    instructing it to land two lanes that seat still held the leases on, one of
    them under an open FIX verdict. The measurement was TRUE (it WAS the newest
    entry); the claim bound to it was false.

    The fork case that justified the fallback is now carried by the SEAT key,
    which survives a session-id fork — so the protection is kept without the
    lottery. What is refused is only what was never provable: an entry naming a
    different seat is dropped, and an entry that can prove NOTHING is dropped
    too, because the sentence built from it asserts ownership.

    The failure direction decides the tie: an unattributed entry costs the
    reader a re-ground, and a foreign one costs a cross-seat collision.
    `reason` is returned rather than swallowed so the caller can tell "no
    handoff exists" from "a handoff exists and is not yours" — silently
    identical answers to different questions is the same class of defect."""
    d = os.path.join(home.project_dir(project), "journal") if project else None
    if not d:
        return None, None, NO_SHELF
    try:
        names = os.listdir(d)
    except FileNotFoundError:
        return None, None, NO_SHELF      # no shelf at all is an honest absence
    except OSError:
        # THE DIRECTORY REFUSED ENUMERATION (chmod 000, a broken mount). This
        # was `glob.glob`, which swallows the error and returns [] — byte
        # identical to "the shelf is empty", so an unreadable SHELF reported
        # exactly like no handoff existing. The same false absence the entry
        # arm below already fixed, one level up, and glob's silence is what
        # hid it. codex, gate 0b18e133f272a1e3.
        return None, None, UNREADABLE
    # dotfiles excluded to keep glob's own semantics: `*handoff*.md` never
    # matched `.handoff.md`, and this is a refactor of HOW the names are read,
    # never of WHICH names count.
    paths = sorted(os.path.join(d, n) for n in names
                   if not n.startswith(".")
                   and fnmatch.fnmatch(n, "*handoff*.md"))
    if not paths:
        return None, None, NO_SHELF
    from . import seats
    own = _own_seat()
    rows, fresh, foreign, unreadable = [], 0, 0, 0
    for p in paths:
        try:
            mt = os.stat(p).st_mtime
        except FileNotFoundError:
            continue                 # vanished between glob and stat — gone,
        except OSError:              # not hidden; nothing to report
            unreadable += 1
            continue
        if floor is not None and mt < floor:
            continue
        meta = pk.parse_simple_frontmatter(p, _ENTRY_KEYS)
        if meta is None:
            # None means the OPEN/READ raised — `parse_simple_frontmatter`
            # returns a defaults dict for a file it can read but that carries
            # no frontmatter, so this is unambiguously unreadable and never a
            # merely-uninteresting file. Skipping it silently is what made a
            # chmod-000 handoff report as "no handoff was written" (codex,
            # gate c8fd53acb0cc435f).
            unreadable += 1
            continue
        fresh += 1
        wrote = str(meta.get("seat") or "")
        # `wrote` first: seats.foreign_seat("") answers True for any declaring
        # caller (""  != own), and an ABSENT stamp is an unproven author, not a
        # different one — the two get different reasons below.
        if wrote and seats.foreign_seat(wrote):   # provably another seat's
            foreign += 1
            continue
        if bool(sid) and meta.get("session_id") == sid:
            rows.append((mt, p, meta, BY_SID))
        elif wrote and own and wrote.casefold() == own.casefold():
            rows.append((mt, p, meta, BY_SEAT))
    if rows:
        # NEWEST wins among entries already proven this caller's. sid used to
        # outrank mtime because it was the only proof of ownership; once the
        # seat key is a second proof, ranking by proof TYPE resumes a seat on
        # the older of its OWN entries — reachable in exactly the case the seat
        # key exists for, since a forked session id writes a second file
        # (<date>-handoff-<sid8>.md) and both files are honestly mine.
        best = max(rows, key=lambda r: (r[0], r[1]))
        return best[1], best[2], best[3]
    # UNREADABLE OUTRANKS EVERY OTHER EMPTY ANSWER, because it is the only one
    # that might have been THIS seat's own handoff. "none was written", "one
    # exists and is not yours" and "one exists and could not be read" are three
    # different facts; the third must not reach a reader dressed as either of
    # the first two. A readable proven entry still wins over an unreadable
    # stranger — the seat resumes on what it can prove, and hears about the
    # rest only when there is nothing to resume on.
    if unreadable:
        return None, None, UNREADABLE
    if not fresh:
        return None, None, NONE_FRESH
    return None, None, FOREIGN_ONLY if foreign else UNATTRIBUTED


def latest_entry(project, sid=None, floor=None):
    """Back-compat 2-tuple over `attribute_entry` — (path, meta) or
    (None, None). A caller that must distinguish "nothing was written" from
    "something was written and it is not yours" calls `attribute_entry`
    directly; collapsing those two into one empty answer is how a reader ends
    up reporting a false absence."""
    path, meta, _reason = attribute_entry(project, sid=sid, floor=floor)
    return path, meta


def hollow(path):
    """The DONE/REMAINING/NEXT sections an authored handoff FAILS to carry.

    () means the contract is discharged — or that the artifact is not an
    authored handoff entry at all (a repo HANDOFF_NEXT_SESSION.md, a legacy
    journal row), which this refuses to judge rather than failing loudly on
    something it does not own.

    WHY THIS EXISTS. `check` answered the contract question by proving a FILE
    EXISTED, was recent, and named the session — and then printed "contract
    satisfied". `write` had already warned, to a different stream at a
    different time, that not one section had parsed. So a handoff carrying no
    DONE, no REMAINING and no NEXT was reported to the next window as a
    satisfied contract, which is worse than reporting nothing: the window that
    trusts it stops looking. Measured on this integrator's own handoff,
    2026-07-29. Existence is not discharge.
    """
    return hollow_checked(path)[0]


def hollow_checked(path):
    """((missing sections), err) — the same answer, plus whether we could LOOK.

    () means the contract is discharged, and an unreadable artifact used to
    return exactly that: a handoff nobody could open reported as SATISFIED,
    which is the worst direction this module has, because the window that
    trusts it stops looking. () with an err set means UNDECIDED, and the caller
    must say so rather than pick either answer.

    The `_checked` shape is helm's existing idiom for exactly this, not a new
    convention: `seats.roster_checked` and `eventledger.checked_events` both
    hand back (value, err) so an unreadable source cannot pass as an empty
    one. codex, gate dcf603c8b0bef585 — the third rung of one class, after the
    unreadable ENTRY and the unreadable SHELF."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(65536)
    except OSError as exc:
        return (), "%s could not be read (%s)" % (path, type(exc).__name__)
    if "type: handoff" not in text:
        return (), None      # not an authored entry: refuses to judge, as ever
    s = _summaries(_FRONTMATTER.sub("", text, count=1))
    return tuple(k for k in ("done", "remaining", "next")
                 if not s.get(k)), None


def check(sid, cwd, start, allow_hollow=False):
    """The contract question -> the satisfying artifact's path, or None.

    `allow_hollow=True` asks the WEAKER question — does an entry exist at all —
    so a caller can tell "you never wrote one" apart from "you wrote one that
    carries nothing". Those need different words: the first asks for a handoff,
    the second asks for a REWRITE, and collapsing them sends an author looking
    for a file that is already there.
    A journal entry on cwd's project shelf CARRYING the session id, or the
    repo's HANDOFF_NEXT_SESSION.md — modified after `start`, the FLOOR the
    caller chose: session start for the standing question, `compaction_floor`
    for a compaction event. start=None reads as now-48h (the standing
    freshness constant): with no known start only a RECENT artifact can
    satisfy — an old handoff must never read as done."""
    strict, _m, hollow, _e = check_checked(sid, cwd, start)
    return (strict or (hollow[0] if hollow else None)) if allow_hollow else strict


def contract_state(sid, cwd, floor):
    """(strict path, loose path, missing sections, unread) — the contract
    question asked ONCE,
    for every surface that asks it.

    WHY THIS EXISTS, and it is the cure for a review spiral rather than a tidy-
    up. The question has always been asked TWICE — strictly, then again with
    allow_hollow — and each surface had to remember to use the CHECKED form for
    BOTH calls. Three review rounds found three different sites where one of
    the two was still bare (codex, gates dcf603c8 / d56a646e / 4ef1aeee), and
    each round's fix was correct and incomplete, because a per-case fix cannot
    close a per-case hole: the next bare call is always outside the set you
    just enumerated.

    So the two looks live HERE and the surfaces consume the answer. One
    mutation now covers every surface at once, which is what makes the next
    pass provably last rather than hopefully last. `unread` folds BOTH passes:
    a read that failed only on the later one is exactly the case the bare
    second call kept losing."""
    found, _fm, hollow, unread = check_checked(sid, cwd, floor)
    loose, missing = (hollow if hollow else (None, ()))
    # THE CLASSIFICATION COMES BACK WITH THE SCAN, so no surface re-reads the
    # entry to find out WHICH sections are missing. Both renderers used to call
    # hollow_checked again and discard its err (`missing, _herr = ...`), which
    # is a fresh swallow standing exactly where the last one was removed —
    # codex named it in the enumeration. Folding herr into `unread` means the
    # error has nowhere to be dropped, and returning `missing` means there is
    # no second read to get wrong.
    return found, (None if found else loose), (() if found else missing), unread


def check_checked(sid, cwd, start):
    """(satisfying path or None, its MISSING sections, err) — the contract
    answer, the classification from the SAME read, and whether every place it
    had to look could actually be read.

    THE CLASSIFICATION RIDES THE SCAN. This loop already calls hollow_checked
    on the entry it is about to return — that is how it decides whether to skip
    a hollow one — so handing the result back is free, and asking again is a
    SECOND READ OF A FILE THAT CAN CHANGE BETWEEN THEM. codex found the race
    (gate 73b7f2cecb441f8f): an entry completed between the two reads yields an
    EMPTY missing-set, and the renderer then prints "carries no " with nothing
    named — a REWRITE instruction for a handoff that is already fine. My
    previous pass claimed to eliminate that re-read and only MOVED it, out of
    the two renderers and into contract_state, where it was equally unpinned.

    None with err=None means NO ARTIFACT, and the nag says the next window
    starts blind. None with an err means WE COULD NOT TELL, and saying "blind"
    there is a false absence about a file that may exist and be perfectly
    good — the same defect as the unreadable ENTRY and the unreadable SHELF,
    two layers further out (codex, gate dcf603c8b0bef585).

    Three places swallowed a read failure into a negative: the journal
    DIRECTORY (an unreadable dir became "no entries"), each ENTRY inside it,
    and the repo HANDOFF file's stat. A found path still answers with err
    None — once we have an artifact, what we could not read no longer decides
    anything."""
    if start is None:
        start = time.time() - FRESH_H * 3600
    unread, hollow_hit = [], None
    project = _project(cwd)
    if project and sid:
        d = os.path.join(home.project_dir(project), "journal")
        try:
            names = sorted(os.listdir(d), reverse=True)  # newest date first
        except FileNotFoundError:
            names = []                      # no shelf is an honest absence
        except OSError as exc:
            names = []
            unread.append("journal dir %s (%s)" % (d, type(exc).__name__))
        for n in names:
            p = os.path.join(d, n)
            try:
                if not n.endswith(".md") or os.stat(p).st_mtime < start:
                    continue
                with open(p, encoding="utf-8", errors="replace") as f:
                    if sid not in f.read(65536):
                        continue
                # A hollow entry is exactly as useful to the next window as no
                # entry, so it must not satisfy the question — otherwise the
                # nag goes quiet precisely when the handoff needs rewriting.
                # An UNDECIDED one must not satisfy it either, and must not
                # vanish silently: it is recorded and reported as unknown.
                missing, err = hollow_checked(p)
                if err:
                    unread.append(err)
                    continue
                if missing:
                    # A hollow entry is exactly as useful to the next window as
                    # no entry, so it does NOT satisfy the question — but it is
                    # a DIFFERENT fact from absence and the caller needs both.
                    # REMEMBERED here rather than found by a second, looser
                    # scan: the newest hollow entry wins, and the walk goes on
                    # looking for one that actually satisfies.
                    if hollow_hit is None:
                        hollow_hit = (p, missing)
                    continue
                return p, (), hollow_hit, None
            except FileNotFoundError:
                continue                    # vanished mid-scan: gone, not hidden
            except OSError as exc:
                unread.append("%s (%s)" % (p, type(exc).__name__))
                continue
    top = _git(cwd, "rev-parse", "--show-toplevel") or cwd
    rp = os.path.join(top, REPO_FILE) if top else None
    try:
        if rp and os.stat(rp).st_mtime >= start:
            return rp, (), hollow_hit, None
    except FileNotFoundError:
        pass                                # simply absent
    except OSError as exc:
        unread.append("%s (%s)" % (rp, type(exc).__name__))
    return None, (), hollow_hit, ("; ".join(unread) if unread else None)


def _nag(sid, captured, stale=None):
    """The PreCompact nag — a FEW lines (PreCompact fires late), a nag never
    a capture: the agent authors the audited prose itself.

    `stale` is the artifact that satisfies the STANDING question but not this
    EVENT's floor. Naming it is the whole difference between "you never wrote
    one" and the live 2026-07-28 case ("you wrote one 37h ago and it is about
    to be handed to the resume leg as this window's directive")."""
    if stale:
        head = ("helm handoff: the newest handoff for session %s is %s old (%s) "
                "— it predates this compaction, so the next window resumes on a "
                "stale directive." % ((sid or "?")[:8],
                                      _age(time.time() - os.stat(stale).st_mtime),
                                      os.path.basename(stale)))
    else:
        head = ("helm handoff: NO handoff artifact for session %s — the next "
                "window starts blind." % ((sid or "?")[:8]))
    out = [head,
           "  author one NOW: helm handoff write   (stdin: DONE/REMAINING/NEXT prose "
           "-> the project journal shelf)"]
    if captured:
        out.append("  automatic snapshot taken — the next session reads it via: "
                   "helm now show")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_USAGE = """usage: helm handoff check [--hook-json] [--session S]
       helm handoff write [--project P] [--session S]   (stdin: DONE/REMAINING/NEXT prose)
       helm handoff recover <sid>"""

_NOW_USAGE = """usage: helm now capture [--hook-json] [--session S]
       helm now show"""


def _opt(rest, flag):
    if flag in rest:
        i = rest.index(flag)
        v = rest[i + 1] if i + 1 < len(rest) else None
        del rest[i:i + 2]
        return v


def cmd_handoff(args):
    """handoff check|write|recover <sid> — the compaction-continuity contract."""
    args = list(args)
    if not args:
        print(_USAGE, file=sys.stderr)
        return 2
    verb, rest = args[0], args[1:]
    # HELP SHORT-CIRCUITS BEFORE ANY VERB READS STDIN. `write` blocks on stdin
    # and `--help` is not a section, so `handoff write --help` used to consume
    # the flag as CONTENT and answer "empty stdin — nothing to hand off": the
    # one question a help flag asks is the one question that reply cannot be an
    # answer to. Same shape as `gate run --help` joining the FIFO (#161), which
    # is why this guards the whole verb table rather than the `write` branch.
    # Clean help is exit 0 on stdout, matching cli.py's tail guard.
    if "-h" in rest or "--help" in rest:
        print(_USAGE)
        return 0
    session = _opt(rest, "--session")
    project = _opt(rest, "--project")

    if verb == "check":
        if "--hook-json" in rest:
            # PreCompact/SessionEnd: fail-open TOTAL, rc 0 always, silent when
            # satisfied; a garbled payload nags nobody (record.py's gate).
            try:
                d = _hook(sys.stdin.read())
                if not d:
                    return 0
                sid = str(d.get("session_id") or "") or session
                cwd = str(d.get("cwd") or "") or os.getcwd()
                tp = str(d.get("transcript_path") or "")
                captured = capture(sid, cwd)  # leg 2 rides the same trigger
                start = _session_start(tp)
                # A COMPACTION IS AN EVENT, and gets the event floor. SessionEnd
                # keeps the standing session-start question: nothing is being
                # discarded mid-flight there, the window simply closes.
                floor = (compaction_floor(start, tp)
                         if str(d.get("hook_event_name") or "") == "PreCompact"
                         else start)
                found, loose, missing, unread = contract_state(sid, cwd, floor)
                if found is None and unread:
                    # UNREADABLE IS NOT ABSENT. Nagging "the next window starts
                    # blind" about a shelf we could not open is a false absence
                    # in the one direction that trains an author to ignore the
                    # guard: they wrote the handoff, and the guard says they
                    # did not. Say what actually happened instead.
                    print("helm handoff: whether a handoff exists is UNKNOWN — "
                          "%s. This is NOT 'no handoff was written'; fix the "
                          "read (permissions, a broken mount) and re-check, and "
                          "do not author a second handoff on the strength of "
                          "this line." % unread)
                elif found is None:
                    # HOLLOW AND ABSENT ASK FOR DIFFERENT THINGS, and the hook
                    # path collapsed them: it only ever asked the strict
                    # question, so an entry that EXISTS and reads hollow was
                    # nagged as "no handoff artifact — the next window starts
                    # blind". Measured on a real, current, richly-written
                    # handoff whose only sin was a heading the parser did not
                    # accept. Telling an author to write a file they already
                    # wrote is how a guard trains people to ignore it.
                    if loose:
                        print("helm handoff: %s exists but carries no %s — "
                              "REWRITE it (helm handoff write); a handoff with "
                              "no sections is as useful to the next window as "
                              "none" % (os.path.basename(loose),
                                        "/".join(missing).upper()))
                    elif str(d.get("hook_event_name") or "") == "PreCompact":
                        # THE THIRD LOOK, and the closure audit caught it on
                        # its first run — it was still calling the checked seam
                        # directly and dropping its err into `_serr`. That is
                        # the same swallow, in the same function, three rounds
                        # after the first one was fixed; I would not have found
                        # it by looking, which is the whole argument for pinning
                        # the enumeration instead of the instances.
                        stale, _sl, _sm, stale_unread = contract_state(
                            sid, cwd, start)
                        print(_nag(sid, captured, stale=stale))
                        if stale_unread:
                            print("helm handoff: (and the STANDING-question "
                                  "scan could not read %s — the nag above may "
                                  "be missing a stale artifact it would "
                                  "otherwise name)" % stale_unread)
                    else:
                        print(_nag(sid, captured))
            except Exception:
                pass
            return 0
        sid = session or home.session_id()
        from . import seats
        # deleted cwd: check runs project-less instead of tracebacking
        # (eager-getcwd class)
        path, loose, missing, unread = contract_state(sid, seats.safe_cwd(), None)
        if path:
            print("helm handoff: contract satisfied — %s (%s old)"
                  % (path, _age(time.time() - os.stat(path).st_mtime)))
            return 0
        if unread:
            # rc 2, not 1: "you have no handoff" and "I could not find out"
            # are different answers and a script must be able to tell them
            # apart. rc 1 already means the contract is UNMET. UNKNOWN also
            # OUTRANKS a hollow find: a readable-but-hollow entry does not
            # rule out that the entry we could NOT read was the good one, so
            # telling this author to rewrite would be a guess.
            print("helm handoff: contract state UNKNOWN — %s\n"
                  "  this is NOT 'no handoff exists'; fix the read and "
                  "re-check rather than writing a second handoff" % unread,
                  file=sys.stderr)
            return 2
        if loose:
            print("helm handoff: entry exists but carries no %s — %s\n"
                  "  a handoff with no sections is as useful to the next "
                  "window as none; rewrite it: helm handoff write"
                  % ("/".join(missing).upper(), loose), file=sys.stderr)
            return 1
        print(_nag(sid, None))
        return 1

    if verb == "write":
        if sys.stdin.isatty():
            print(_USAGE, file=sys.stderr)
            return 2
        text = sys.stdin.read()
        if not text.strip():
            print("helm handoff write: empty stdin — nothing to hand off",
                  file=sys.stderr)
            return 2
        proj = _project(os.getcwd(), project)
        if not proj:
            print("helm handoff: no registry project claims this cwd — pass "
                  "--project <name>", file=sys.stderr)
            return 1
        sid = session or home.session_id()
        path, missing = write_entry(text, proj, sid)
        if path is None:
            print("helm handoff: REFUSED — no %s section parsed; first line "
                  "that would not parse: %r\n"
                  "  nothing written — the contract wants DONE/REMAINING/NEXT; "
                  "rewrite and re-run: helm handoff write"
                  % ("/".join(missing).upper(), _misread(text, missing)),
                  file=sys.stderr)
            return 1
        print("helm handoff: journal entry landed — " + path)
        print("  the next window confirms via `helm handoff check`; "
              "`helm ship` carries it cross-machine")
        return 0

    if verb == "recover":
        sid = next((a for a in rest if not a.startswith("--")), None)
        if not sid or not _SAFE_SID.match(sid):
            print(_USAGE, file=sys.stderr)
            return 2
        import subprocess
        cmdv = ["cv", "show", sid, "--pre-compaction"]
        try:
            return subprocess.run(cmdv, timeout=120,
                                  env=home.cv_env()).returncode
        except FileNotFoundError:
            print("helm handoff recover: cv not on PATH — the one recall index "
                  "(architecture law 4); run: " + " ".join(cmdv), file=sys.stderr)
            return 1
        except Exception as e:
            print("helm handoff recover: cv failed (%s)" % e, file=sys.stderr)
            return 1

    print("helm handoff: unknown subverb %r" % verb, file=sys.stderr)
    print(_USAGE, file=sys.stderr)
    return 2


def cmd_now(args):
    """now capture|show — the automatic session-continuity snapshot."""
    args = list(args)
    if not args:
        print(_NOW_USAGE, file=sys.stderr)
        return 2
    verb, rest = args[0], args[1:]
    # Same guard as cmd_handoff, and placed here for the same reason: the
    # defect is the VERB TABLE answering a help flag with work, so a cure that
    # only covered the one verb observed failing would be the per-case handling
    # this repo keeps re-learning to avoid.
    if "-h" in rest or "--help" in rest:
        print(_NOW_USAGE)
        return 0
    session = _opt(rest, "--session")

    if verb == "capture":
        hook = "--hook-json" in rest
        sid, cwd = None, os.getcwd()
        if hook and not sys.stdin.isatty():
            d = _hook(sys.stdin.read())
            sid = str(d.get("session_id") or "") or None
            cwd = str(d.get("cwd") or "") or cwd
        sid = sid or session or home.session_id()
        path = capture(sid, cwd)
        if hook:
            return 0  # a safety net is silent and never blocks
        if path is None:
            print("helm now: capture failed (fail-open — nothing written)",
                  file=sys.stderr)
            return 1
        print("helm now: snapshot -> " + path)
        return 0

    if verb == "show":
        text = show()
        if text:
            print(text)
        return 0

    print("helm now: unknown subverb %r" % verb, file=sys.stderr)
    print(_NOW_USAGE, file=sys.stderr)
    return 2
