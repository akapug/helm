"""A model run's own record on disk (task/2966): where a Claude Code Agent or
Workflow run left its transcript, and what that transcript proves.

A model run is not a seat. It has no roster row and no pane, so the only thing
that can show it ran, what model answered, whose session spawned it and what
it touched is the record the harness wrote. This module finds that record by
the run id and reads it. It never writes anything.

WHERE THE RECORDS ARE, MEASURED ON ONE HOST: 4195 Agent transcripts and 1241
Workflow run records under the Claude project roots, and every one of them in
these shapes:

  Agent tool run  <root>/<cwd-slug>/<session>/subagents/agent-<id>.jsonl,
                  <id> = `a` + 16 hex, beside agent-<id>.meta.json
  Workflow run    <root>/<cwd-slug>/<session>/workflows/<run>.json,
                  <run> = `wf_` + 8 hex + `-` + 3 hex; each of its agents at
                  <session>/subagents/workflows/<run>/agent-<id>.jsonl

<session> is the session that SPAWNED the run, and every transcript line names
it again as `sessionId`. The roots are the session catalog's
(`catalog.CLAUDE_ROOTS`) plus every helm seat home, the same two sets the
persistence census reads (`session._persisting_sids`).

A FORK IS NOT A FRESH CONTEXT. A fork subagent starts from its parent's whole
conversation. Measured: its meta says agentType `fork` and isFork true, and its
transcript opens with a `fork-context-ref` record. Either mark refuses the run
as a fresh-context read, and so does a missing meta file, because without it a
fork cannot be told from a fresh run.

WHAT THE RECORD SAYS, AND WHAT IT CANNOT. It says the run exists, which
session spawned it, which models the harness says answered, every Write,
Edit, MultiEdit and NotebookEdit it made, and whether any line of it names
the reviewed tip. An edit is a write to the lane only in the shared checkout
or the lane worktree: a reader's cure committed in its own clone, as the
review procedure says, is not one (`touches`). A transcript's model field is what the harness logged, not
an attestation of the weights, so a model that CONTRADICTS the declared
reader refuses and a matching one is only consistent. A shell command that
edits a file is not a Write or an Edit, and this reader does not see it:
when the run began is what covers it (below).

A RUN ALIVE WHILE THE LANE WAS WRITTEN MAY HAVE WRITTEN IT (task/3658).
With the spawner bound gone, the Write/Edit check is what tells a reader
from the lane's builder, and it sees only those four tools, on lane files,
in the lane's checkouts. A builder subagent that edited through the shell
(sed -i, a heredoc, python), or in a clone of its own, and committed, left
none of them. A builder necessarily ran while the lane was written; a
fresh reader starts after the tip exists. So a run must also have BEGUN
after the reviewed tip was committed: its earliest transcript line's
`timestamp` is later than the tip's committer time, which git reads in the
first lane checkout that holds the tip (`tip_committed`), with replacement
objects off, so a `refs/replace/<tip>` cannot date another commit in the
tip's place. The refusal names the line that began first. git keeps that
time to the second, so a run that began in the tip's own second cannot be
told to have begun after it, and is refused. A run with no timestamp, a
stamp that names no instant, and a tip no checkout can date are refused
too. The committer time is the committer's clock, as unattested as the
transcript (below). Measured on one host, over the 1500 newest Agent and
Workflow agent transcripts: every one of their 1,013,533 lines carries a
`timestamp`, an ISO 8601 string ending in `Z`, and each transcript's first
line holds its earliest.

A RUN'S LINEAGE IS ITS OWN, never its spawner's (task/3658). A subagent
that is no fork starts from its brief, so the session that spawned it holds
none of its context, whatever that session wrote or continued. What the
record can show is the run's own lines: a fork mark on one of them
(`FORK_MARKS`), or a conversation it continues before any turn of its own
(a `summary` line, a compacted turn, a compact boundary), which carries a
context this record does not hold. Each refuses. A run that compacts its
own context later keeps every line before the boundary (measured on the
host: a compacted Agent transcript kept its 2528 earlier lines), so the
Write/Edit check reads all of it and the compaction holds only the run's
own context.

THE RECORD IS UNATTESTED. Nothing in it is signed. A process running as the
same user can run `claude -p` under a session no roster names, write a
transcript in the shapes above, or point HELM_CLAUDE_ROOTS at a directory it
wrote, and this reader cannot tell that record from a real one. So a record
that passes every check here is RECORDED (UNATTESTED): consistent with a
fresh-context read, and never proof of one. Only signed provenance could
attest it. The began-after bound trusts the committer date, which
GIT_COMMITTER_DATE can set into the past, so a builder that backdated its
commit could pass as its own fresh reader; a normal amend, rebase or
cherry-pick stamps the current time.

A RUN IS A READ OF ONE TIP. A clean Opus run exists on disk for every lane a
seat ever had read, so a run id whose record passes is not yet this row's
read: it must name the reviewed tip somewhere in its transcript (its brief
does, and so does every git tool result that printed the sha). A run whose
transcript never names the tip read some other lane, and is refused as this
one's.

A RUN IN FLIGHT HAS GIVEN NO READ. What a run still working names now may
not be what it concludes, so only a finished run passes. An Agent run has
finished when the last user or assistant line of its transcript is an
assistant line whose stop reason ends the turn (FINAL_STOPS); a Workflow
run, when its record says it completed (it also ends killed or failed).

UNKNOWN REFUSES. A run id in neither shape, no record, two distinct records, a
malformed transcript line, a record whose lines name another session, a
workflow agent with no transcript, a run still in flight, a run with no
usable timestamp, or a tip no checkout can date are all refusals that name
what was missing.
"""
import datetime
import glob
import json
import os
import re
import time

#: An Agent tool run id, with or without the file name's `agent-` prefix.
AGENT_RUN = re.compile(r"(?:agent-)?(a[0-9a-f]{16})\Z")
#: A Workflow run id.
WORKFLOW_RUN = re.compile(r"wf_[0-9a-f]{8}-[0-9a-f]{3}\Z")
#: The tools that write a file. A run that used one on a lane file wrote part
#: of the lane, so its read of that lane is not independent of it.
EDIT_TOOLS = frozenset(("Write", "Edit", "MultiEdit", "NotebookEdit"))
#: The model the harness logs for its own synthetic turns (an API error, an
#: interrupted turn). It names no model that answered.
SYNTHETIC_MODEL = "<synthetic>"
#: The stop reasons a finished Agent run's last turn ends on. Measured on one
#: host, over the Agent transcripts older than an hour written by harness
#: 2.1.270 or later: 649 of 664 end on end_turn or stop_sequence, followed at
#: most by hook attachments. The other 15 end on a tool call or on a line
#: with no stop reason, which is also how a run still working looks: a tool
#: call awaiting its result (tool_use), a result it has not answered yet (a
#: user line), or an assistant line still being written (no stop reason).
FINAL_STOPS = frozenset(("end_turn", "stop_sequence"))
#: The status a Workflow run record carries when the run finished its work.
#: Measured: 1173 records completed, 86 killed and 4 failed.
WORKFLOW_DONE = "completed"


def roots():
    """Every Claude project root on this host: the session catalog's roots and
    each helm seat home's `projects` directory."""
    from . import catalog, home
    patterns = list(catalog.CLAUDE_ROOTS)
    seats = os.path.join(home.global_dir(), "seats")
    patterns += [os.path.join(seats, "*", "claude", "projects"),
                 os.path.join(seats, "*", "instances", "*", "claude",
                              "projects")]
    found = []
    for pattern in patterns:
        found.extend(p for p in glob.glob(pattern) if os.path.isdir(p))
    return found


def locate(run, where=None):
    """(record, error) — the one record the run id names on disk.

    record = {"kind": "agent"|"workflow", "run", "session", "record",
              "transcripts": [paths], "metas": [paths]}. `where` is the list of
    project roots to search (default `roots()`). Two roots that are one
    directory (a credential home whose projects link back to ~/.claude) find
    one record; two DISTINCT files for one id are an ambiguity and refuse."""
    run = str(run or "").strip()
    agent = AGENT_RUN.fullmatch(run)
    if agent:
        kind, rel = "agent", os.path.join(
            "subagents", "agent-%s.jsonl" % agent.group(1))
    elif WORKFLOW_RUN.fullmatch(run):
        kind, rel = "workflow", os.path.join("workflows", run + ".json")
    else:
        return None, ("run id %r is neither an Agent run (a + 16 hex, the "
                      "agent id) nor a Workflow run (wf_ + 8 hex - 3 hex)"
                      % run[:40])
    where = roots() if where is None else list(where)
    hits = {}
    for root in where:
        for path in glob.glob(os.path.join(glob.escape(root), "*", "*", rel)):
            hits.setdefault(os.path.realpath(path), path)
    if not hits:
        return None, _absent(run, kind, agent, where)
    if len(hits) > 1:
        return None, ("run %s has %d distinct records on disk (%s), so which "
                      "one is the read cannot be told"
                      % (run, len(hits), ", ".join(sorted(hits))))
    record = next(iter(hits))
    session_dir = os.path.dirname(os.path.dirname(record))
    out = {"kind": kind, "run": run, "record": record,
           "session": os.path.basename(session_dir), "status": None}
    if kind == "agent":
        out["transcripts"] = [record]
    else:
        agents, out["status"], err = _workflow_agents(record)
        if err:
            return None, "workflow run %s: %s" % (run, err)
        wdir = os.path.join(session_dir, "subagents", "workflows", run)
        out["transcripts"] = [os.path.join(wdir, "agent-%s.jsonl" % a)
                              for a in agents]
        missing = [p for p in out["transcripts"] if not os.path.isfile(p)]
        if missing:
            return None, ("workflow run %s lists agent(s) with no transcript "
                          "on disk: %s" % (run, ", ".join(missing)))
    out["metas"] = [p[:-len(".jsonl")] + ".meta.json"
                    for p in out["transcripts"]]
    return out, None


def _absent(run, kind, agent, where):
    """Why no record was found, naming the two near misses measured on the
    host: a Workflow still running (its agents' directory exists, and the run
    record is written only when the run ends), and an agent id that belongs
    to a Workflow (whose run id is the one to name)."""
    if kind == "workflow":
        near = os.path.join("subagents", "workflows", run)
        if any(glob.glob(os.path.join(glob.escape(r), "*", "*", near))
               for r in where):
            return ("Workflow run %s has no run record yet: it is still "
                    "running, and its record is written when it ends. "
                    "Record the read once it has finished" % run)
    else:
        near = os.path.join("subagents", "workflows", "*",
                            "agent-%s.jsonl" % agent.group(1))
        for root in where:
            for path in glob.glob(os.path.join(glob.escape(root), "*", "*",
                                               near)):
                return ("agent %s is one agent of Workflow run %s: name the "
                        "Workflow run" % (agent.group(1), os.path.basename(
                            os.path.dirname(path))))
    return ("no record of run %s under %d Claude project root(s): the run's "
            "transcript does not exist on this host" % (run, len(where)))


def _workflow_agents(path):
    """([agent id], status, error) — the agents a Workflow run record lists,
    and the status it ended with."""
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, ValueError) as exc:
        return None, None, "its run record cannot be read (%s)" % exc
    data = data if isinstance(data, dict) else {}
    progress = data.get("workflowProgress")
    agents = []
    for item in progress if isinstance(progress, list) else ():
        if isinstance(item, dict) and item.get("type") == "workflow_agent":
            agent = str(item.get("agentId") or "")
            if not AGENT_RUN.fullmatch(agent):
                return None, None, "it lists an agent with no usable id"
            if agent not in agents:
                agents.append(agent)
    if not agents:
        return None, None, "its run record lists no agent"
    status = data.get("status")
    return agents, status if isinstance(status, str) else None, None


def read(record, tip=None):
    """(facts, error) — what the run's transcripts and metas say.

    facts = {"models": set, "edits": [(tool, path)], "sessions": set,
             "fork": bool, "names_tip": bool, "last_turn": (type, stop),
             "lineage": why or None,
             "began": (seconds, stamp, where) or None}.
    Every line must parse: a line that does not is a record this reader
    cannot vouch for, including a run still writing. `names_tip` is whether
    any line carries the first twelve hex of `tip`, the prefix every helm
    surface prints. `last_turn` is the last user or assistant line's type
    and, for an assistant line, its stop reason (None when there is none).
    `lineage` is the first line that shows the run is no conversation of
    its own (`_lineage`), or None. `began` is the earliest `timestamp` of
    any line of any of its transcripts, in epoch seconds, as the line wrote
    it, and the transcript line that carries it, or None when no line
    carries one; a line whose timestamp names no instant is a record this
    reader cannot vouch for."""
    facts = {"models": set(), "edits": [], "sessions": set(), "fork": False,
             "names_tip": False, "last_turn": None, "lineage": None,
             "began": None}
    needle = str(tip or "").strip().casefold()[:12]
    for meta in record["metas"]:
        try:
            with open(meta, encoding="utf-8") as stream:
                data = json.load(stream)
        except (OSError, ValueError) as exc:
            return None, ("its meta file %s cannot be read (%s), so a fork "
                          "cannot be told from a fresh run" % (meta, exc))
        if not isinstance(data, dict):
            return None, "its meta file %s is not an object" % meta
        if data.get("isFork") is True or data.get("agentType") == "fork":
            facts["fork"] = True
    for path in record["transcripts"]:
        own = False
        try:
            with open(path, encoding="utf-8") as stream:
                for number, line in enumerate(stream, 1):
                    if not line.strip():
                        continue
                    if needle and needle in line.casefold():
                        facts["names_tip"] = True
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        return None, ("its transcript %s has a malformed "
                                      "line %d (a run still writing is read "
                                      "once it has finished)" % (path, number))
                    where = "%s line %d" % (path, number)
                    if isinstance(entry, dict):
                        facts["lineage"] = facts["lineage"] or _lineage(
                            entry, own, where)
                        own = own or entry.get("type") == "assistant"
                    err = _fold(entry, facts, where)
                    if err:
                        return None, "its transcript %s %s" % (where, err)
        except (OSError, UnicodeError) as exc:
            return None, "its transcript %s cannot be read (%s)" % (path, exc)
    return facts, None


def _instant(stamp):
    """Epoch seconds of an ISO 8601 instant that names its zone (the
    harness writes `...Z`), or None: a stamp with no zone, no date or no
    string cannot be placed in time."""
    if not isinstance(stamp, str):
        return None
    text = stamp.strip()
    try:
        when = datetime.datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text)
    except ValueError:
        return None
    return when.timestamp() if when.utcoffset() is not None else None


def _fold(entry, facts, where=None):
    """Fold one transcript line, `where` (its path and number), into
    `facts`; the reason it cannot be, or None."""
    if not isinstance(entry, dict):
        return None
    if "timestamp" in entry:
        when = _instant(entry["timestamp"])
        if when is None:
            return ("carries a timestamp %s that names no instant, so when "
                    "the run began cannot be told" % repr(
                        entry["timestamp"])[:64])
        if facts["began"] is None or when < facts["began"][0]:
            facts["began"] = (when, entry["timestamp"], where)
    if entry.get("type") == "fork-context-ref":
        facts["fork"] = True
    session = entry.get("sessionId")
    if isinstance(session, str) and session:
        facts["sessions"].add(session)
    message = entry.get("message")
    if entry.get("type") in ("user", "assistant"):
        stop = message.get("stop_reason") if isinstance(message, dict) \
            and entry["type"] == "assistant" else None
        facts["last_turn"] = (entry["type"], stop if isinstance(stop, str)
                              else None)
    if entry.get("type") != "assistant" or not isinstance(message, dict):
        return
    model = message.get("model")
    if isinstance(model, str) and model and model != SYNTHETIC_MODEL:
        facts["models"].add(model)
    for block in message.get("content") or ():
        if isinstance(block, dict) and block.get("type") == "tool_use" \
                and block.get("name") in EDIT_TOOLS:
            given = block.get("input")
            given = given if isinstance(given, dict) else {}
            facts["edits"].append((block["name"], given.get("file_path")
                                   or given.get("notebook_path")))


def _in_flight(record, facts):
    """Why the run has not finished its read, or None when it has."""
    if record["kind"] == "workflow":
        if record.get("status") == WORKFLOW_DONE:
            return None
        return ("Workflow run %s ended %s, not %s, so it finished no read"
                % (record["run"], record.get("status") or "with no status",
                   WORKFLOW_DONE))
    turn = facts["last_turn"]
    if turn and turn[0] == "assistant" and turn[1] in FINAL_STOPS:
        return None
    what = ("no user or assistant turn" if not turn else
            "a user turn it has not answered (a tool result, or a message "
            "that resumed it)" if turn[0] == "user" else
            "a tool call awaiting its result" if turn[1] == "tool_use" else
            "an assistant line with no final stop reason (%s)"
            % (turn[1] or "still being written"))
    return ("run %s has not finished: its transcript ends on %s, not a final "
            "answer, so it is in flight and has given no read yet. Record "
            "the read once the run has finished" % (record["run"], what))


def touches(path, lane_files, checkouts):
    """Did a Write or Edit of `path` change one of the lane's files IN the
    lane?

    The lane lives in its `checkouts`: the shared checkout and the lane's own
    worktree (review_door.lane_checkouts). A reader following the review
    procedure commits its cure to a lane file in a clone of its own, off the
    reviewed tip; that edit is the procedure, not a write to the lane, so an
    absolute path counts only inside a lane checkout, where its path
    relative to that checkout must be a lane file. An edit nobody can place
    still counts: no path at all, or a relative path that is a lane file
    (or ends in one), since which checkout it stood in cannot be told."""
    if not isinstance(path, str) or not path.strip():
        return True
    path = os.path.normpath(path.strip())
    files = {os.path.normpath(f) for f in lane_files}
    if not os.path.isabs(path):
        return any(path == rel or path.endswith(os.sep + rel)
                   for rel in files)
    for place in {path, os.path.realpath(path)}:
        for checkout in checkouts:
            for base in {os.path.normpath(checkout),
                         os.path.realpath(checkout)}:
                if place.startswith(base.rstrip(os.sep) + os.sep) \
                        and os.path.relpath(place, base) in files:
                    return True
    return False


#: The fields the harness marks a forked conversation with, as session
#: doctor reads them: the parent a forked session names.
FORK_MARKS = ("forkedFrom", "forked_from")


def _continues(entry):
    """Does this line carry an earlier conversation into this one: a
    continuation's `summary` line, a compacted turn, a compact boundary?"""
    return (entry.get("type") == "summary" or entry.get("isCompactSummary")
            or entry.get("subtype") == "compact_boundary")


def _lineage(entry, own, where):
    """Why one line of a run's own transcript shows it is no conversation of
    its own, or None (task/3658): a fork mark, or a continuation before any
    assistant turn of its own (`own`), whose context this record does not
    hold. `where` names the line."""
    marks = [m for m in FORK_MARKS if entry.get(m)]
    if marks:
        return ("its transcript records a fork (%s, %s): it began from "
                "another conversation" % (", ".join(marks), where))
    if _continues(entry) and not own:
        return ("its transcript continues an earlier conversation before any "
                "turn of its own (%s): it carries a context this record does "
                "not hold" % where)
    return None


def tip_committed(tip, checkouts):
    """(epoch seconds, None) — when the reviewed tip `tip` was committed, as
    git reads its committer time in the first of `checkouts` that holds it,
    or (None, why) naming each checkout that could not. Repository selection
    in the environment is removed (`vcs._authority_env`), so each checkout
    answers for itself, and replacement objects are off (`vcs._NO_REPLACE`),
    so git dates the object the tip names: a `refs/replace/<tip>` would
    date another commit in its place (measured: a replacement committed two
    hours earlier moved a plain `git show` of the tip two hours back)."""
    from . import vcs
    env = dict(vcs._authority_env(), **vcs._NO_REPLACE)
    tried = []
    for checkout in checkouts or ():
        rc, out, err = vcs.backend(checkout).text(
            checkout, "show", "-s", "--format=%ct", tip + "^{commit}",
            timeout=10, env=env)
        if rc == 0 and out.isdigit():
            return int(out), None
        tried.append("%s: %s" % (checkout, " ".join(
            (err or out or "git exited %d" % rc).split())[:120]))
    return None, ("no checkout of the lane can read when the reviewed tip %s "
                  "was committed (%s), so whether the run began after it "
                  "cannot be told" % (tip[:12], "; ".join(tried)
                                      or "none given"))


def _began_after(run, facts, tip, checkouts):
    """Why run `run` did not begin after the reviewed tip was committed, or
    None: its earliest line (`facts["began"]`) must fall in a later second
    than the tip's committer time (`tip_committed`), which git keeps to the
    second. A builder necessarily ran while the lane was written; a fresh
    reader starts after the tip exists. The refusal names the line that
    began first, since a Workflow has one transcript per agent."""
    if facts["began"] is None:
        return ("run %s: no line of its transcript carries a timestamp, so "
                "whether it began after the reviewed tip %s was committed "
                "cannot be told" % (run, tip[:12]))
    committed, err = tip_committed(tip, checkouts)
    if err:
        return "run %s: %s" % (run, err)
    began, stamp, where = facts["began"]
    if int(began) > committed:
        return None
    return ("run %s began at %s, %s the reviewed tip %s was committed at %s: "
            "a run alive while the lane was being written may have written it "
            "(its earliest line is %s)"
            % (run, stamp, "before" if began < committed else "in the second",
               tip[:12], time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                       time.gmtime(committed)), where))


def verify(run, lane_files, model, tip, checkouts, where=None):
    """(record + facts, error) — the run a verdict names, whose record on
    disk is consistent with a fresh-context read of `tip` by `model` that
    wrote none of the lane's files. It is a check of an unattested record
    (see the module docstring), never proof.

    `model` is a compiled pattern the transcript's models must all match;
    `tip` is the reviewed tip, an exact sha, which some line of the
    transcript must name. A run still in flight is refused: it has given
    no read yet. `checkouts` are where the lane lives (the shared checkout
    and the lane worktree): only an edit there is a write to the lane, and
    git dates the tip in the first that holds it. The run must have begun
    after the tip was committed (`_began_after`): a builder that edited
    through the shell leaves no Write or Edit, and began before its tip.
    Each refusal names the bound it failed: (b) the record, (c) the run's
    own lineage (`_lineage`). The session that spawned the run is no bound
    (task/3658). The tip comes last, so every other refusal keeps its
    name."""
    tip = str(tip or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", tip):
        return None, ("(b) the reviewed tip %r is no exact sha, so what the "
                      "run read cannot be told" % tip[:40])
    record, err = locate(run, where)
    if err:
        return None, "(b) " + err
    facts, err = read(record, tip)
    if err:
        return None, "(b) run %s: %s" % (run, err)
    if facts["fork"]:
        return None, ("(b) run %s is a FORK: it started from its parent's "
                      "whole context, so it is no fresh-context read" % run)
    err = _in_flight(record, facts)
    if err:
        return None, "(b) " + err
    others = sorted(facts["sessions"] - {record["session"]})
    if others:
        return None, ("(b) run %s lives under session %s but its transcript "
                      "names %s, so whose run it is cannot be told"
                      % (run, record["session"], ", ".join(others)))
    if facts["lineage"]:
        return None, "(c) run %s: %s" % (run, facts["lineage"])
    if not facts["models"]:
        return None, ("(b) run %s: no assistant turn in its transcript names "
                      "a model" % run)
    wrong = sorted(m for m in facts["models"]
                   if not model.search(m.casefold()))
    if wrong:
        return None, ("(b) run %s: its transcript names model(s) %s, which "
                      "contradicts the declared reader"
                      % (run, ", ".join(wrong)))
    wrote = [(tool, path) for tool, path in facts["edits"]
             if touches(path, lane_files, checkouts)]
    if wrote:
        return None, ("(b) run %s made %s to the lane's own files in the "
                      "shared checkout or the lane worktree (%s): a run "
                      "that wrote the lane did not only read it"
                      % (run, "/".join(sorted({t for t, _p in wrote})),
                         ", ".join(sorted({str(p) for _t, p in wrote}))))
    err = _began_after(run, facts, tip, checkouts)
    if err:
        return None, "(b) " + err
    if not facts["names_tip"]:
        return None, ("(b) run %s: no line of its transcript names the "
                      "reviewed tip %s, so it read some other lane, not this "
                      "one at that tip" % (run, tip[:12]))
    return dict(record, **facts), None
