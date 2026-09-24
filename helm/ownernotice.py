"""The owner's fleet notice, and the one posture snapshot every surface reads.

WHAT THE OWNER ASKED FOR (task/3018): an away mode he can set from the web
console, which active agents learn about at their NEXT check-in, never by
being woken, plus a short notice to the whole fleet ("coordinate through a2a;
do not narrate in local chat; save output tokens"). He does not type verbs, so
the web console is his door.

TWO FACTS, TWO FILES, ONE DIRECTORY.
  * AWAY is away.py's flag, and only away.py writes it (`declare` / `lift`).
    Nothing here writes or copies that boolean: a second record of it is how
    two surfaces come to disagree about where a person is.
  * The NOTICE is his words to the fleet. It is a different fact (it can
    stand while he is here, and he may be away with nothing to say), so it
    has its own file, NOTICE_NAME, beside the flag in the same private
    directory and under the same hardening. It holds ONE notice, with its
    text, the time it was set, his name and the door it came through. A new
    notice REPLACES the old one; clearing it removes the file. Lifting away
    does not clear it, and `helm back` says so when one is still standing.

WHO MAY WRITE THE NOTICE. Only the owner, through one of his own doors: the
writer takes an ownerasks.OwnerDoor, a capability only the web console's
owner handlers mint (a census arm fails any other minting site), and refuses
a string. The argv-guard's owner-door rung refuses an agent's shell command
that POSTs to the web door. Every agent runs as the owner's uid, so a process
that writes the file by hand is outside this threat model, exactly as
ownerasks states for its own ledger; what the reader does add is that a
record naming no owner door is IGNORED, shown on the web card as such, and
never rendered to a seat.

THE RESIDUAL, STATED, because this file is read by every seat as his word. A
DELIBERATE same-uid process can still render as the owner: it can read the
bearer off the served page and POST to the away card, mint an OwnerDoor in a
script file it runs, or write a flag or notice that names his web door. The
fence stops a seat doing it by ACCIDENT, which is helm's threat model (an
agent, or a prompt-injected one, through ordinary tooling, not an adversary
on the box): the argv-guard refuses a command that mints the door, writes
either file by hand, or deletes the notice by hand
(chat.owner_posture_forge_refusal), and the owner-door
rung refuses a shell POST to the card. Closing the deliberate route needs a
secret only the web server holds, and nothing here pretends to one.

HOW A SEAT LEARNS IT: `turn_lines`, called by the per-turn inject. The inject
already keeps a per-session seen-memory (inject-seen/<session>.json); the
posture's two component fingerprints ride in it, so a change renders ONCE in
each context at that context's next working turn and not on the turns after.
Lifting renders a one-time "owner is BACK" line to a context that saw him
away. Two changes between one seat's turns render the LATEST state once,
because the render reads state, not a log of events. Nothing here posts a
chat row, mentions anyone or touches a room, so a beacon sees nothing and no
idle seat wakes.

UNREADABLE IS UNKNOWN. Every reader here answers set / none / unknown (and
ignored for a notice no owner door wrote). An unreadable flag or notice is
never drawn as "present" or "no notice".
"""
import calendar
import hashlib
import json
import os
import tempfile
import time

from . import away, pk

NOTICE_NAME = "owner-notice"

#: The longest notice the console accepts, in characters. A notice rides into
#: every seat's context once per change; it is a sentence, not a brief.
NOTICE_MAX = 240

#: The presets the card offers. ONE on purpose: the owner asked for this one,
#: and more are ideas for him to pick, not a system to build ahead of him.
PRESETS = (
    {"id": "a2a", "label": "Away: a2a only",
     "text": "Away: coordinate through a2a (helm chat / dispatch); do not "
             "narrate in local chat; save output tokens."},
)

SET, NONE, UNKNOWN, IGNORED = "set", "none", "unknown", "ignored"

#: What every rendered line starts with, so a seat can tell it from a rule.
TAG = "[helm posture]"

#: The ledger id the inject records when a posture line is delivered.
POSTURE_ID = "whisper:owner-posture"

#: The byte ceiling on all posture lines of one turn. They sit OUTSIDE the
#: arrival cap (like the pinned contract), because they are the owner's word
#: and ride once per change; this cap is their own bound.
POSTURE_CAP = 640

#: How much of an UNKNOWN reason a line carries.
WHY_MAX = 120

#: What a line cut at POSTURE_CAP ends with: the seat's memo records the cut
#: line as seen, so it must say where the whole notice is.
CUT_TAIL = "\u2026 (full notice: helm away status)"

REFUSE_FORGED = ("refusing: a fleet notice is the owner's word, and only his "
                 "web console writes or clears one. An agent that needs him "
                 "posts in helm chat")


# ---------------------------------------------------------------------------
# the notice file
# ---------------------------------------------------------------------------

def notice_path():
    """The notice's path: the away flag's directory, so both facts share one
    private, hardened place. Raises away.MarkerUnresolvable when the store
    cannot be located (readers turn that into UNKNOWN)."""
    return os.path.join(os.path.dirname(away.marker_path()), NOTICE_NAME)


def _epoch(ts):
    try:
        return calendar.timegm(time.strptime(str(ts), "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError, OverflowError):
        return None


def _fp(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str)
                          .encode("utf-8")).hexdigest()[:16]


def _owner_rails():
    from .seats import OWNER_RAILS
    return OWNER_RAILS


def read_notice(path=None):
    """The standing notice -> dict with `state` SET / NONE / UNKNOWN / IGNORED.

    SET carries text, by, door, ts, t (epoch) and id. UNKNOWN and IGNORED
    carry `why`. O_NOFOLLOW for the reason away.declared_by gives: a symlink
    at the path is not the owner's notice, and following it would render any
    file on the box to every seat. Reads; never writes."""
    try:
        path = path or notice_path()
    except away.MarkerUnresolvable as e:
        return {"state": UNKNOWN, "why": str(e)}
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        return {"state": UNKNOWN,
                "why": "this runtime cannot refuse to follow a symlink"}
    try:
        fd = os.open(path, os.O_RDONLY | nofollow)
    except FileNotFoundError:
        return {"state": NONE}
    except OSError as e:
        return {"state": UNKNOWN, "why": "cannot read the notice at %s: %s"
                % (path, e.strerror or e.__class__.__name__)}
    try:
        with os.fdopen(fd, "rb") as fh:
            raw = fh.read(8192)
        rec = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        return {"state": UNKNOWN, "why": "the notice at %s is unreadable (%s)"
                % (path, e.__class__.__name__)}
    return _typed(rec)


def _typed(rec):
    text = rec.get("text") if isinstance(rec, dict) else None
    if not isinstance(text, str) or not text.strip() or len(text) > NOTICE_MAX:
        return {"state": UNKNOWN, "why": "the notice file does not hold a "
                                         "notice this console wrote"}
    door = rec.get("door")
    if door not in _owner_rails():
        return {"state": IGNORED, "why": "the file names door %r, which is "
                "not one of the owner's doors" % (door,)}
    ts = rec.get("ts") if isinstance(rec.get("ts"), str) else None
    by = rec.get("by") if isinstance(rec.get("by"), str) else ""
    return {"state": SET, "text": text, "by": by, "door": door, "ts": ts,
            "t": _epoch(ts), "id": _fp(text, by, door, ts)}


def _is_door(by):
    from .ownerasks import OwnerDoor
    return isinstance(by, OwnerDoor)


def write_notice(text, by):
    """Set the fleet notice, replacing any standing one. -> (notice, None) on
    a write that read back as SET, else (None, why) and nothing changed.

    `by` must be an ownerasks.OwnerDoor. Whitespace (newlines included) is
    folded to single spaces: a notice is one line in a seat's context."""
    if not _is_door(by):
        return None, REFUSE_FORGED
    text = " ".join(str(text or "").split())
    if not text:
        return None, "a notice needs some words"
    if len(text) > NOTICE_MAX:
        return None, ("a notice is at most %d characters and this one is %d"
                      % (NOTICE_MAX, len(text)))
    try:
        path = notice_path()
    except away.MarkerUnresolvable as e:
        return None, "cannot set the notice: %s" % e
    d = os.path.dirname(path)
    err = away._harden_parent(d)
    if err:
        return None, err
    from .seats import owner_name
    ts = pk.now_ts()
    rec = {"v": 1, "text": text, "by": owner_name(), "door": by.door,
           "ts": ts}
    # PUBLISHED WHOLE: a temp file in the same directory, then one rename.
    # A reader sees the old notice or the new one, never half of either.
    # REPLACE, NOT LINK: unlike the away flag, a second notice is meant to
    # supersede the first.
    try:
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".notice-", suffix=".tmp")
    except OSError as e:
        return None, "could not stage the notice in %s: %s" % (d, e)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    except OSError as e:
        return None, "could not write the notice at %s: %s" % (path, e)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    back = read_notice(path)
    if back.get("state") != SET or back.get("text") != text:
        return None, ("the notice was written, but reading it back did not "
                      "show it, so agents may not see it: %s"
                      % (back.get("why") or back.get("state")))
    return back, None


def clear_notice(by):
    """Remove the fleet notice. -> ("cleared" | "already" | "refused", why).
    Only through an OwnerDoor: clearing his notice silences his word, which
    is as much his to do as setting it."""
    if not _is_door(by):
        return "refused", REFUSE_FORGED
    try:
        path = notice_path()
    except away.MarkerUnresolvable as e:
        return "refused", "cannot clear the notice: %s" % e
    try:
        os.remove(path)
    except FileNotFoundError:
        return "already", None
    except OSError as e:
        return "refused", "could not clear the notice at %s: %s" % (path, e)
    return "cleared", None


def quote(text):
    return "“%s”" % (text or "")


# ---------------------------------------------------------------------------
# the snapshot every surface reads
# ---------------------------------------------------------------------------

def _split_declarer(line):
    """"name (via)" -> (name, via); the marker's own spelling."""
    line = (line or "").strip()
    if line.endswith(")") and " (" in line:
        name, _, via = line[:-1].rpartition(" (")
        return name.strip(), via.strip()
    return line or None, None


def read_away():
    """The away flag as the snapshot shows it: state, and when AWAY, who
    declared it, through which door or provenance, and since when (the flag's
    own mtime: the declaration wrote it, so no clock is consulted)."""
    state, why = away.state()
    out = {"state": state}
    if why:
        out["why"] = why
    if state == away.AWAY:
        name, via = _split_declarer(away.declared_by())
        out["by"] = name
        out["via"] = via
        out["owner_door"] = via in _owner_rails()
        try:
            st = os.lstat(away.marker_path())
            out["t"] = int(st.st_mtime)
            out["ts"] = pk.epoch_ts(st.st_mtime)
        except (OSError, away.MarkerUnresolvable):
            pass
    # THE FINGERPRINT IS THE POSTURE, NOT THE FILE: away, who, through what.
    # A lift and a re-declaration by the same door between one seat's turns
    # leave that seat's context exactly as true as it was, so it hears nothing.
    out["fp"] = _fp(state, out.get("by"), out.get("via"), why)
    return out


def snapshot():
    """{"away": ..., "notice": ...}: the one read the web card, `helm away
    status` and the per-turn inject all make. Never raises for a readable or
    unreadable store alike; a broken store reads UNKNOWN."""
    n = read_notice()
    n["fp"] = _fp(n.get("state"), n.get("id"), n.get("why"))
    return {"away": read_away(), "notice": n}


def _hhmm(ts):
    return ts[11:16] + "Z" if isinstance(ts, str) and len(ts) >= 16 else "?"


def _cut(text, n):
    text = str(text or "")
    return text if len(text) <= n else text[:n - 1] + "…"


def _away_line(a):
    if a.get("owner_door"):
        return ("%s The owner is AWAY (set %s from his %s console). Keep "
                "working; coordinate through a2a (helm chat, dispatch), keep "
                "local-chat narration short and save output tokens. /afk "
                "holds the full contract."
                % (TAG, _hhmm(a.get("ts")), a.get("via") or "web"))
    return ("%s AWAY was declared %s by %s (%s), not from an owner door. "
            "Treat the owner as away; coordinate through a2a (helm chat, "
            "dispatch). /afk holds the full contract."
            % (TAG, _hhmm(a.get("ts")), a.get("by") or "an unnamed process",
               a.get("via") or "no provenance"))


def turn_lines(snap, prior):
    """(lines, memo) for one working turn of one context.

    `prior` is the memo this context last recorded (from the inject's
    seen-memory), or None for a context that has recorded none. `memo` is what
    to record now. A component whose fingerprint matches its memo renders
    nothing, so an unchanged posture costs zero bytes; a changed one renders
    the CURRENT state once. A context that never heard of an away posture is
    never told the owner is back."""
    a, n = snap.get("away") or {}, snap.get("notice") or {}
    memo = {"away": [a.get("state"), a.get("fp")],
            "notice": [n.get("state"), n.get("fp")]}
    prior = prior if isinstance(prior, dict) else {}
    pa, pn = prior.get("away"), prior.get("notice")
    lines = []
    if not (pa and pa[1] == a.get("fp")):
        if a.get("state") == away.AWAY:
            lines.append(_away_line(a))
        elif a.get("state") == away.UNKNOWN:
            lines.append("%s Whether the owner is away is UNKNOWN (%s). Do "
                         "not assume he is here or away."
                         % (TAG, _cut(a.get("why"), WHY_MAX)))
        elif pa and pa[0] == away.AWAY:
            lines.append("%s The owner is BACK: away was lifted, and the /afk "
                         "posture no longer applies." % TAG)
        elif pa and pa[0] == away.UNKNOWN:
            lines.append("%s The away flag reads again: the owner is not "
                         "marked away." % TAG)
    if not (pn and pn[1] == n.get("fp")):
        if n.get("state") == SET:
            lines.append("%s Owner notice (set %s from his %s console): %s"
                         % (TAG, _hhmm(n.get("ts")), n.get("door") or "web",
                            n.get("text")))
        elif n.get("state") == UNKNOWN:
            lines.append("%s The owner's notice is UNKNOWN (%s)."
                         % (TAG, _cut(n.get("why"), WHY_MAX)))
        elif pn and pn[0] == SET:
            # NOT "the owner cleared it": the file's absence does not say who
            # removed it (any same-uid process can), and a record no owner
            # door wrote is his notice REPLACED, not cleared. What a seat can
            # be told truthfully is that the notice it holds no longer stands.
            if n.get("state") == IGNORED:
                lines.append("%s The owner's fleet notice was replaced on "
                             "disk by a record no owner door wrote; it is "
                             "not his word and is not shown, and his earlier "
                             "notice no longer stands." % TAG)
            else:
                lines.append("%s The owner's fleet notice was cleared and no "
                             "longer stands." % TAG)
    return _fit(lines), memo


def _fit(lines):
    """Keep whole lines within POSTURE_CAP; a line that does not fit is cut
    and ends with CUT_TAIL, which names where the whole text is: the memo
    records the cut line as seen, so this seat is never sent the rest. (A
    line is dropped only when too few bytes remain for the tail, which needs
    a first line over ~560 B; the away and UNKNOWN lines measure under 215 B,
    so in practice only a long non-ASCII notice is ever cut.)"""
    out, used = [], 0
    tail = len(CUT_TAIL.encode("utf-8"))
    for line in lines:
        b = len(line.encode("utf-8")) + (1 if out else 0)
        if used + b <= POSTURE_CAP:
            out.append(line)
            used += b
            continue
        room = POSTURE_CAP - used - (1 if out else 0) - tail
        if room > len(TAG) + 8:
            out.append(line.encode("utf-8")[:room]
                       .decode("utf-8", "ignore").rstrip() + CUT_TAIL)
        break
    return out


def status_lines(snap):
    """`helm away status`: the card's facts as two lines."""
    a, n = snap["away"], snap["notice"]
    if a["state"] == away.AWAY:
        where = ("from the %s console" % a.get("via") if a.get("owner_door")
                 else "at the command line (%s)" % (a.get("via")
                                                   or "no provenance"))
        first = "away:   AWAY since %s, set by %s %s" % (
            a.get("ts") or "an unknown time", a.get("by") or "an unnamed "
            "process", where)
    elif a["state"] == away.PRESENT:
        first = "away:   not marked away"
    else:
        first = ("away:   UNKNOWN (%s); this is not the same as present"
                 % a.get("why"))
    if n["state"] == SET:
        second = "notice: %s, set %s by %s from the %s console" % (
            quote(n["text"]), n.get("ts") or "at an unknown time",
            n.get("by") or "the owner", n.get("door"))
    elif n["state"] == NONE:
        second = "notice: none"
    elif n["state"] == IGNORED:
        second = ("notice: IGNORED (%s); agents are not shown it"
                  % n.get("why"))
    else:
        second = ("notice: UNKNOWN (%s); this is not the same as none"
                  % n.get("why"))
    return [first, second]


def web_model():
    """What GET /api/owner/posture answers: the snapshot, the presets and the
    notice's length limit. The card draws these and decides nothing."""
    snap = snapshot()
    return {"away": snap["away"], "notice": snap["notice"],
            "presets": [dict(p) for p in PRESETS], "notice_max": NOTICE_MAX}
