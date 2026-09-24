"""WHAT THE WATCHDOG BELIEVED, WRITTEN DOWN BEFORE IT STOPS BELIEVING IT.

proxywatch's record is a SNAPSHOT: one file plus a single .last-good backup,
replaced wholesale each pass. The falsification fields -- when the belief was
first observed, which proxy process it was observed against, whether the bar
was reached -- are carried only while a family is in cooldown, so the moment a
wall CLEARS every one of them is simply not carried forward and the record that
replaces it has no trace they existed.

THE AUDIT WINDOW IS THEREFORE EXACTLY THE OUTAGE, which is the one window in
which nobody is auditing. While the wall is up you can ask how long the belief
has been held and whether the proxy identity changed underneath it. The second
it clears -- which is when anyone would sit down to ask whether the machinery
behaved -- the answers are gone. An actuator whose trigger leaves no evidence
cannot be evaluated after the fact, and task/2092's RESTART_HELPFUL actuator is
built on exactly this belief.

SO THE END OF AN EPISODE IS A WRITE, NOT A LONGER RETENTION. Retaining the
fields on a healthy record would be worse than losing them: a stale observation
clock rendered beside a recovered family is a surface that lies. One appended
line at the transition says what was believed and for how long, and the healthy
record stays honest about the present.

A HEALTHY WATCH WRITES NOTHING. A line here means an episode ENDED, so the file
contains only what somebody would want to read about -- the same rule stopprobe
uses for a ladder that never went slow.

THE GRAMMAR IS THE SIBLING'S and so is the code: probelog owns how a line is
written and read, hookprobe.log and stopprobe.log are the other files in it, and
anything reading one can read the others.

AND THE READER SHIPS WITH THE WRITER. The instrument beside this one held 359
unread records for thirteen days while the question they answered stayed open.
`read` and `episodes` are the point of the file, not an accessory.
"""
from . import probelog

END = "PROXY-FALSIFY-END"     # a cooldown episode stopped being believed
MALFORMED = "PROXY-BAD"       # OUR OWN line, torn or truncated: never dropped
_ORDER = (END,)

# WHAT AN END RECORD MUST CARRY TO BE WORTH KEEPING. A line that cannot name
# the family and the state it left answers no question anyone would ask of it,
# so the writer refuses it rather than filing an unreadable record.
_REQUIRED = {END: ("family", "was", "now")}
_NUMERIC = ("age_s",)


def log_path():
    """Resolved PER CALL and anchored on helm's OWN home.

    A module constant built from `~` cannot see HELM_HOME, so a hermetic test
    would write into the real file -- the contamination that cost the sibling
    instrument 532 of 2586 records.
    """
    import os
    from . import home
    override = os.environ.get("HELM_PROXYJOURNAL_LOG")
    if override:
        return override
    return os.path.join(home.helm_home(), "helm", "pause-ops",
                        "proxywatch.log")


def record_episode_end(family, before, now_state, log=None, seat=None):
    """Append what a cooldown episode believed, at the moment it stops.

    Returns True when a line was written and False when there was nothing to
    lose -- so a caller can assert the SILENCE as easily as the record, which
    is what keeps "a healthy watch writes nothing" a tested property rather
    than a claim in this docstring.
    """
    before = before if isinstance(before, dict) else {}
    observed = before.get("falsification_observed_at")
    identity = before.get("falsification_proxy_identity")
    if observed is None and identity is None:
        return False
    fields = [("family", family or ""),
              ("was", before.get("state") or ""),
              ("now", now_state or ""),
              ("observed_at", observed if observed is not None else ""),
              ("proxy_identity", identity if identity is not None else ""),
              ("identity_state", before.get("falsification_identity_state")
               if before.get("falsification_identity_state") is not None
               else ""),
              ("age_s", before.get("falsification_age_s")
               if before.get("falsification_age_s") is not None else ""),
              ("due", before.get("falsification_due")
               if before.get("falsification_due") is not None else "")]
    fields = [(k, v) for k, v in fields if str(v) != ""]
    return probelog.write(log or log_path(), END, fields,
                          required=_REQUIRED[END], known_seat=seat)


def read(log=None):
    """-> (rows, err). `err` is a sentence when the log could not be READ.

    A MISSING FILE IS NOT AN EMPTY ONE. A fleet in which no wall has ever
    cleared and an instrument that was never wired produce the same zero rows;
    only the error channel can separate them.
    """
    path = log or log_path()
    return probelog.read(
        path, _ORDER, _REQUIRED, _NUMERIC, MALFORMED,
        absent="no proxy falsification journal at %s — UNMEASURED, not quiet"
               % path)


def episodes(rows):
    """Ended episodes per family, newest last, MALFORMED rows excluded.

    What the caller gets is what was BELIEVED, never a claim about what was
    true: `due` says the bar was reached, not that anything acted on it.
    """
    out = {}
    for row in rows:
        if row["event"] == MALFORMED:
            continue
        out.setdefault(row.get("family", ""), []).append(row)
    return out


def malformed(rows):
    """OUR OWN records that arrived torn. Never silently dropped."""
    return [r for r in rows if r["event"] == MALFORMED]
