#!/usr/bin/env python3
"""The qwen27 findings pass: every review row gets one local read, and the
result lands on the row as a NOTE for the approving reviewer to adjudicate.

THE OWNER'S CONTRACT (task/2960), which every choice below serves:

  * It runs on EVERY review row, and it is always on. `HELM_QWEN27_FINDINGS`
    set to `0`, `off` or `no` switches it off.
  * It is NEVER an approval, NEVER a gate, and NEVER the different-model read.
    The note is projected as `findings_notes` on the row, nothing that reads a
    verdict, an advisory read or a family reads that key, and the advisory-read
    door refuses this model outright (`dispatches._on_behalf_shape`).
  * An empty result is NOT a clean review, and the note says so.
  * If the reader is down or slow the row gets ONE line naming why, never a
    hold. Filing never waits for it and never fails because of it.
  * It costs no credential: the model runs on the owner's own box.

WHO OWNS WHAT. The script that does the reading, `local-review.py`, belongs to
another project on the operator's host and runs IN PLACE from its checkout:
`HELM_LOCAL_REVIEW_SCRIPT` names it, else `local-review-script` in this host's
local names (helm/localnames.py). Named nowhere, the pass notes why and reads
nothing. This module starts it, queues
it, bounds it and maps its answer to one note. The ledger event and its reducer live in
`helm/dispatches.py`, because that module owns every event kind on the
dispatch ledger.

DETACHED AND QUEUED. `queue` starts `python3 -m helm.findingspass <row id>` in
its own session, so the filing verb returns at once and the pass outlives it.
The worker then takes ONE flock under the helm home before it reads anything,
so one pass runs at a time on this host: the reader's server has four slots
shared with other users, and a second concurrent pass would only slow both.
Everything heavy is imported AFTER the lock, so a queued worker costs a
sleeping interpreter and nothing more.

THE ENDPOINT IS THE CATALOG'S. It is resolved from seat_catalog's qwen27 entry
on every run — the pool provider `pool_default` names, else the first one the
entry lists — plus `/chat/completions`. No host is written here, so a move of
the serving stack is one catalog edit.

Import-safe and stdlib-only.
"""
import fcntl
import os
import re
import signal
import subprocess
import sys
import time
import unicodedata

from . import home

READER = "qwen27"
#: The switch, the script path and the run's wall bound. New knobs, so no
#: legacy spelling is read (docs/ENVIRONMENT.md).
SWITCH = "HELM_QWEN27_FINDINGS"
SCRIPT = "HELM_LOCAL_REVIEW_SCRIPT"
TIMEOUT = "HELM_QWEN27_FINDINGS_TIMEOUT_S"
#: A 30-file row measured 10-20 minutes; the bound is three times the top of
#: that, and a run past it is stopped and noted, never waited on.
DEFAULT_TIMEOUT_S = 3600
CHECKLIST = os.path.join("docs", "preread-checklist.md")
STORE = os.path.join(".state", "findings-pass")
LOCK = "queue.lock"
LOG = "worker.log"
LOG_CAP = 1 << 20
#: The script's exit code for each answer it gives (its calling contract).
EXIT_STATUS = {0: "complete", 2: "partial", 3: "unread"}
STATUS = re.compile(r"^LOCAL-REVIEW-STATUS (complete|partial|unread)"
                    r"((?: [a-z_]+=\d+)*)\s*$")
#: A kept finding, verbatim: the judge's REAL marker and the reader's text,
#: up to the next block the script writes. Nothing else in the output is a
#: finding (the script's own contract).
FINDING = re.compile(r"\*\*\[JUDGE: REAL \d+/\d+\]\*\*\n.*?"
                     r"(?=\n\*\*\[|\n<details>|\n## |\n---\n|\Z)", re.S)
_SECTION = re.compile(r"^## (.+)$", re.M)
#: The tail lines that name what a PARTIAL read left unread.
_PARTIAL_LINES = ("READER ERRORS", "EMPTY ANSWERS", "CUT AT --max-tokens",
                  "TRUNCATED, so")


def enabled():
    """The switch. On unless it says 0, off or no."""
    return str(os.environ.get(SWITCH, "1")).strip().lower() \
        not in ("0", "off", "no")


def store_dir():
    return os.path.join(home.global_dir(), STORE)


def log_path():
    return os.path.join(store_dir(), LOG)


def script_path():
    """The reading script's path, or "" when nothing names one."""
    from . import localnames
    named = os.environ.get(SCRIPT) or localnames.value("local-review-script")
    return os.path.expanduser(named) if named else ""


def checklist_path():
    return os.path.join(_package_root(), CHECKLIST)


def timeout_s():
    """The run's wall bound in seconds. A value that is not a positive
    integer is ignored and the default stands."""
    raw = str(os.environ.get(TIMEOUT) or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else DEFAULT_TIMEOUT_S


def _package_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def endpoint(table=None):
    """(url, None) or (None, why) — the reader's chat endpoint, from the
    catalog's qwen27 entry. The provider is the one `pool_default` names,
    else the first the entry lists: the rule `helm seat add` already uses.

    EACH ROW IS RESOLVED BY `seat_catalog.pool_base_url`, never by reading
    `base_url` here. A row on the operator's own box carries `base_url_from`
    and no host: its URL lives in the helm home's endpoints file. Reading the
    key directly saw no provider at all for such a row, so every note read
    NOT RUN while the endpoint was configured. An unconfigured endpoint is
    still NOT RUN, with the resolver's own reason, which names the file."""
    from . import seat, seat_catalog  # noqa: F401 — the facade first (seat_compat)
    families = seat_catalog.FAMILIES if table is None else table
    fam = families.get(READER) if isinstance(families, dict) else None
    if not isinstance(fam, dict):
        return None, "seat_catalog has no %s entry" % READER
    pool = fam.get("pool_providers")
    rows, whys = [], []
    for name, row in (pool.items() if isinstance(pool, dict) else ()):
        if not isinstance(row, dict):
            continue
        url, why = seat_catalog.pool_base_url(row)
        if url.startswith(("http://", "https://")):
            rows.append((name, url))
        elif why:
            whys.append("%s: %s" % (name, why))
    if not rows:
        return None, ("seat_catalog's %s entry resolves no pool provider to "
                      "an http endpoint%s" % (READER, "".join(
                          " — " + w for w in whys)))
    chosen = next((url for name, url in rows
                   if name == fam.get("pool_default")), rows[0][1])
    return chosen + "/chat/completions", None


# ---------------------------------------------------------------- queue

#: THE WORKER IS NOBODY'S CHILD. A shell in its own session backgrounds the
#: worker and exits at once, and the filing process reaps that shell: so a
#: long-lived caller collects no zombie, and the worker is adopted by init
#: rather than left to a parent that may exit or signal its own group.
WORKER_ARGV = ["/bin/sh", "-c", 'exec "$@" &', "helm-findings-pass"]


def queue(row, popen=None):
    """(started, why) — start the pass for a review row, DETACHED.

    (True, None) started; (False, None) nothing to do — the switch is off or
    the row is not a review; (False, why) it could not be started, and the
    caller records that on the row."""
    if not enabled() or not isinstance(row, dict) \
            or row.get("kind") != "review":
        return False, None
    rid = str(row.get("id") or "")
    if not re.fullmatch(r"[0-9a-f]{8,64}", rid):
        return False, "the row carries no usable id"
    root = _package_root()
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (root, env.get("PYTHONPATH")) if p)
    try:
        os.makedirs(store_dir(), exist_ok=True)
        with open(os.devnull, "rb") as null, open(log_path(), "ab") as sink:
            shell = (popen or subprocess.Popen)(
                WORKER_ARGV + [sys.executable, "-m", "helm.findingspass",
                               rid],
                stdin=null, stdout=sink, stderr=subprocess.STDOUT,
                start_new_session=True, close_fds=True, cwd=root, env=env)
            shell.wait(timeout=30)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)
    return True, None


def _lock():
    """The fleet's one-at-a-time queue: an exclusive flock, held until the
    returned descriptor is closed. Blocks while another pass runs."""
    os.makedirs(store_dir(), exist_ok=True)
    fd = os.open(os.path.join(store_dir(), LOCK),
                 os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        os.close(fd)
        raise
    return fd


# ---------------------------------------------------------------- the pass


def run(rid, timeout=None):
    """(row, why) — one pass on one row, under the queue lock. The row comes
    back when a note was recorded; `why` says what happened otherwise."""
    fd = _lock()
    try:
        return _run_locked(rid, timeout)
    finally:
        os.close(fd)


def _run_locked(rid, timeout):
    from . import dispatches
    current, unavailable = dispatches.snapshot()
    if unavailable:
        return None, "dispatch ledger unavailable: %s" % unavailable
    row, err = dispatches._resolve_row(current, rid)
    if err:
        return None, err
    if row.get("status") not in dispatches.FINDINGS_NOTE_STATES:
        return None, ("row %s is %s by now: nobody is left to adjudicate a "
                      "note, so the pass is skipped" % (rid[:12],
                                                        row.get("status")))
    tip = str(row.get("tip") or "")
    if any(n.get("reviewed_tip") == tip
           and n.get("outcome") in ("complete", "partial")
           for n in row.get("findings_notes") or () if isinstance(n, dict)):
        return None, "row %s already carries a read of %s" % (rid[:12],
                                                              tip[:12])
    fields, output = examine(tip, row.get("repo_root"), timeout)
    if output:
        ref, nbytes, why = dispatches.write_brief_file(output)
        if why:
            fields["reason"] = "; ".join(
                p for p in (fields.get("reason"),
                            "the whole output was NOT stored: " + why) if p)
        else:
            fields.update(output_ref=ref, output_bytes=nbytes)
    return dispatches.record_findings_note(rid, tip, fields)


def examine(tip, repo, timeout=None):
    """({note field: value}, output text) — run the script once and map its
    answer. Every way it can fail is a note with a reason, never an
    exception and never a silence."""
    started = time.monotonic()
    bound = timeout or timeout_s()

    def done(fields, output=""):
        fields["wall_s"] = int(round(time.monotonic() - started))
        return fields, output

    script = script_path()
    if not script:
        return done(not_run("no local-review script is named: set %s or "
                            "`local-review-script` in the helm home's local "
                            "names" % SCRIPT))
    if not os.path.isfile(script):
        return done(not_run("the local-review script is missing at %s"
                            % script))
    url, why = endpoint()
    if why:
        return done(not_run("no %s endpoint: %s" % (READER, why)))
    checklist = checklist_path()
    if not os.path.isfile(checklist):
        return done(not_run("the checklist is missing at %s" % checklist))
    if not re.fullmatch(r"[0-9a-f]{40,64}", str(tip or "")):
        return done(not_run("the row names no full tip to read"))
    if not repo or not os.path.isdir(repo):
        return done(not_run("the row's checkout %s is not readable here"
                            % (repo or "(none)")))
    # THE SCRIPT WRITES --out LAST, after every read, so a directory that is
    # not there costs the whole run: measured against the real script, it
    # read all five files and then died writing its output with exit 1.
    try:
        os.makedirs(store_dir(), exist_ok=True)
    except OSError as exc:
        return done(not_run("the pass's store %s is not writable: %s"
                            % (store_dir(), exc)))
    out = os.path.join(store_dir(), "run-%d.md" % os.getpid())
    argv = [sys.executable, script, tip, "--judge", "--repo", repo,
            "--checklist", checklist, "--endpoint", url, "--out", out]
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                start_new_session=True)
    except OSError as exc:
        return done(not_run("local-review.py could not be started: %s" % exc))
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=bound)
    except subprocess.TimeoutExpired:
        timed_out = True
        _stop(proc)
        stdout, stderr = proc.communicate()
    text = _decode(stdout)
    written = _read_and_remove(out)
    if written and not timed_out:
        text = written
    fields = classify(proc.returncode, text, _decode(stderr), timed_out,
                      bound)
    return done(fields, text)


def not_run(reason):
    return {"outcome": "not-run", "reason": reason}


def classify(rc, text, stderr="", timed_out=False, bound=None):
    """{note field: value} for one run of the script.

    FAIL-CLOSED, and this is the function the owner's "an empty result is not
    a clean review" rests on: `complete` is returned only when the exit code
    is 0 AND the last status line says complete AND it carries both counts.
    Any disagreement between the exit code and the status line is `failed`,
    never the more reassuring of the two."""
    if timed_out:
        return {"outcome": "timeout",
                "reason": "the pass ran past its %s s bound and was stopped"
                % bound}
    if rc == 1:
        return {"outcome": "not-run", "rc": 1,
                "reason": "local-review.py could not start: %s"
                % (_last_line(stderr) or "it printed no reason")}
    if rc not in EXIT_STATUS:
        return {"outcome": "failed", "rc": rc if isinstance(rc, int) else -1,
                "reason": "local-review.py exited %s, outside its 0/1/2/3 "
                "contract%s" % (rc, (": " + _last_line(stderr))
                                if _last_line(stderr) else "")}
    status, counts, line = parse_status(text)
    if status != EXIT_STATUS[rc] or "reads" not in counts \
            or "kept" not in counts:
        return {"outcome": "failed", "rc": rc,
                "reason": "exit %d says %s, but the status line %s — the "
                "result cannot be read" % (
                    rc, EXIT_STATUS[rc],
                    "is missing" if status is None
                    else "says %s" % status if status != EXIT_STATUS[rc]
                    else "lacks its reads or kept count")}
    fields = {"outcome": status, "rc": rc, "status_line": line,
              "reads": counts["reads"], "kept": counts["kept"]}
    if status == "partial":
        fields["reason"] = partial_reason(counts, text)
    elif status == "unread":
        fields["reason"] = unread_reason(text)
    findings = extract_findings(text)
    if findings:
        fields["findings"] = findings
    return fields


def parse_status(text):
    """(status, {name: count}, line) from the LAST status line, else
    (None, {}, None). The script promises it is the last line; reading the
    last one means a quoted status line inside a finding cannot win."""
    for raw in reversed(str(text or "").splitlines()):
        match = STATUS.match(raw.strip())
        if match:
            counts = {k: int(v) for k, v in
                      re.findall(r"([a-z_]+)=(\d+)", match.group(2))}
            return match.group(1), counts, raw.strip()
    return None, {}, None


def partial_reason(counts, text):
    """What a PARTIAL read left unread: the non-zero counts, then the lines
    the script wrote naming the files."""
    named = [line.strip() for line in str(text or "").splitlines()
             if line.strip().startswith(_PARTIAL_LINES)]
    parts = ["%s=%d" % (k, counts[k]) for k in
             ("errors", "empty", "cut", "truncated") if counts.get(k)]
    return "; ".join(p for p in [" ".join(parts)] + named if p) \
        or "the script called it partial and named nothing"


def unread_reason(text):
    """Why nothing was read: the first reader error the script printed, else
    its own no-read line."""
    lines = [line.strip() for line in str(text or "").splitlines()]
    for line in lines:
        if line.startswith("READER ERROR:"):
            return line
    for line in lines:
        if line.startswith("NO FILE WAS READ"):
            return line
    return "no read succeeded and the script named no error"


def extract_findings(text, cap=None):
    """The kept findings, verbatim, each under the section header it was
    written in, bounded to what a ledger row may carry. A cut is MARKED with
    the size it came from; the whole output is stored by reference beside
    the note. Control characters other than newline and tab are replaced,
    so a model's answer cannot carry a terminal escape onto a screen."""
    from . import dispatches
    cap = cap or dispatches.FINDINGS_TEXT_CAP
    text = str(text or "")
    blocks = []
    for match in FINDING.finditer(text):
        heads = _SECTION.findall(text, 0, match.start())
        head = ("## %s\n" % heads[-1].strip()) if heads else ""
        blocks.append(head + match.group(0).strip())
    if not blocks:
        return None
    whole = _printable("\n\n".join(blocks))
    if len(whole) <= cap:
        return whole
    mark = ("\n[... cut: %d of %d characters shown; the whole output is "
            "stored by reference on this note]")
    room = cap - len(mark % (cap, len(whole))) - 8
    return whole[:room] + mark % (room, len(whole))


def _printable(text):
    return "".join(c if c in "\n\t" or unicodedata.category(c) not in
                   ("Cc", "Cf", "Zl", "Zp") else "?" for c in text)


def _last_line(text):
    lines = [line.strip() for line in str(text or "").splitlines()
             if line.strip()]
    return lines[-1] if lines else ""


def _decode(data):
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data or "")


def _read_and_remove(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return ""
    try:
        os.remove(path)
    except OSError:
        pass
    return text


def _stop(proc):
    """Stop the script and everything it started: TERM to its group, then
    KILL if it has not gone within five seconds."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


# ---------------------------------------------------------------- the note


def summary(note):
    """The ONE line a reader sees first. "No findings" appears only for a
    complete read that kept nothing, and it always says what it is not."""
    reader = note.get("reader") or READER
    outcome = note.get("outcome")
    reads, kept = note.get("reads"), note.get("kept")
    reason = note.get("reason") or "no reason was recorded"
    if outcome == "complete" and kept == 0:
        return ("%s: no findings (complete, %s reads). Not a review, not an "
                "approval." % (reader, reads))
    if outcome == "complete":
        return ("%s: %s finding(s) for the approving reviewer to adjudicate "
                "(complete, %s reads). Not a review, not an approval."
                % (reader, kept, reads))
    if outcome == "partial":
        return ("%s: PARTIAL read, not clean — %s. %s. Not a review, not an "
                "approval." % (reader, reason,
                               "%s finding(s) kept from what was read" % kept
                               if kept else "Nothing was kept from what WAS "
                               "read, which says nothing about the rest"))
    if outcome == "unread":
        return ("%s: ABSENT review (reader down or erroring), not clean — %s."
                % (reader, reason))
    if outcome == "timeout":
        return "%s: NOT FINISHED — %s. Not a review." % (reader, reason)
    if outcome == "not-run":
        return "%s: NOT RUN — %s. Not a review." % (reader, reason)
    if outcome == "failed":
        return ("%s: FAILED — %s. No findings can be read from it; not a "
                "review." % (reader, reason))
    return ("%s: a note with an unknown outcome %r — read it as absent, never "
            "as clean." % (reader, outcome))


def note_lines(row):
    """Every line a reader of this row sees about its findings pass, or []
    when the row carries no note. The newest note speaks; an older tip and
    the count of earlier notes are said, never hidden."""
    from . import dispatches
    notes = [n for n in (row or {}).get("findings_notes") or ()
             if isinstance(n, dict)]
    if not notes:
        return []
    note = notes[-1]
    tip = str(note.get("reviewed_tip") or "")
    lines = ["findings pass (%s, tip %s): %s"
             % (note.get("ts") or "?", tip[:12], summary(note))]
    if tip and tip != str(row.get("tip") or ""):
        lines.append("  this note read an EARLIER tip; the row now names %s"
                     % str(row.get("tip") or "?")[:12])
    for line in _printable(str(note.get("findings") or "")).splitlines():
        lines.append("    " + line)
    if note.get("status_line"):
        lines.append("  " + _printable(note["status_line"]))
    ref = note.get("output_ref")
    if ref:
        _text, problem = dispatches.read_brief_file(ref, note.get("output_bytes"))
        path = dispatches.brief_file_path(ref)
        lines.append("  whole output: %s (%s bytes)" % (path,
                                                        note.get("output_bytes"))
                     if not problem else
                     "  whole output UNAVAILABLE at %s — it is missing or is "
                     "not the text this note recorded" % path)
    if len(notes) > 1:
        lines.append("  (%d earlier note%s on this row)"
                     % (len(notes) - 1, "" if len(notes) == 2 else "s"))
    return lines


# ---------------------------------------------------------------- the worker


def _log(text):
    """One line in the worker log, rotated once past LOG_CAP."""
    path = log_path()
    try:
        if os.path.getsize(path) > LOG_CAP:
            os.replace(path, path + ".1")
    except OSError:
        pass
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                               time.gmtime()), text))
    except OSError:
        pass


def _main(argv):
    """`python3 -m helm.findingspass <row id>` — the detached worker, and the
    hand-run form for re-reading a row once the reader is back."""
    if len(argv) != 1 or argv[0].startswith("-"):
        print("usage: python3 -m helm.findingspass <dispatch-id>",
              file=sys.stderr)
        return 2
    row, why = run(argv[0])
    if row is None:
        _log("%s: no note recorded: %s" % (argv[0][:12], why))
        print("findings pass: no note recorded: %s" % why, file=sys.stderr)
        return 1
    note = (row.get("findings_notes") or ({},))[-1]
    _log("%s: %s" % (argv[0][:12], summary(note)))
    print(summary(note))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
