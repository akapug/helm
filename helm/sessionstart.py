"""How each session BEGAN, as its first SessionStart join recorded it
(task/3483).

The verdict door's bound (c) read it to tell a lane author's fresh session
from a fork or continuation of the one that wrote the lane. Since task/3658
that door judges the reading instance by its own run record, so no door
reads this now; the join still records it. It lives beside the join that
writes it, in the chat state's own family.
"""
import json
import os

from . import chat, pk

# One small file per session, written by the first SessionStart join
# (`seats_join.join`) that names it and never rewritten, so it says how the
# session BEGAN: the harness's SessionStart `source` ("startup" for a new
# conversation; "resume", "clear" or "compact" for one that carries on an
# earlier context, a `--fork-session` included), Claude's own record of the
# process's `kind` (`seats_join.session_kind`), and when. A later start of
# the same id (a resume, a compaction) is not how it began, so it never
# overwrites the first. Like a run record it is unattested: a
# same-user process can write one, so it is consistent with a fresh start
# and never proof of one.
START_FAMILY = "session-start"


def _start_path(session):
    return chat.state_path(START_FAMILY, "start." + pk.slug(str(session)))


def note_session_start(session, source, kind=None, ts=None):
    """Record how `session` began, once; True when this call recorded it.
    Never raises: a session start is never shaped by its record. The record
    is written aside and hard-linked into place, so a reader sees a whole
    record or none, and a second start of the id finds it and writes
    nothing."""
    if not session or not source:
        return False
    path = _start_path(session)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        os.makedirs(chat.state_family_dir(START_FAMILY), mode=0o700,
                    exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as stream:
            json.dump({"session": str(session), "source": str(source),
                       "kind": kind, "ts": ts or pk.now_ts()}, stream)
        os.link(tmp, path)
        return True
    except OSError:
        return False
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def start_state(session):
    """(record, why): how `session` began, or (None, None) when no start is
    recorded, or (None, why) when a record exists but cannot be read or names
    another session, which is UNKNOWN, never taken for a missing one
    (task/3483, N3)."""
    if not session:
        return None, None
    path = _start_path(session)
    if not os.path.lexists(path):
        return None, None
    d = pk.read_json(path, None)
    if isinstance(d, dict) and d.get("session") == str(session):
        return d, None
    return None, ("its start record %s exists but cannot be read"
                  % os.path.basename(path))


def session_start(session):
    """How `session` began, as its first SessionStart join recorded it
    ({"session", "source", "kind", "ts"}), or None: none recorded, or a
    record that cannot be read or names another session."""
    if not session:
        return None
    d = pk.read_json(_start_path(session), None)
    return d if isinstance(d, dict) and d.get("session") == str(session) \
        else None
