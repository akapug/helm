"""A DRIVEN REMOTE SESSION: an agent helm drives but that is not a helm agent.

The class (store premise
`driven-remote-sessions-talk-to-helm-only-through-a-relay`): the session runs
off this box — a Claude Code cloud session today, a container session later —
so it has no beacon, cannot reach the chat node, cannot write the dispatch
ledger and has no local process whose liveness helm can read. THE CHANNEL IS
ASYMMETRIC. helm reaches IN one message at a time (`claude -p MSG --cloud SID`
queues one message and exits with no reply), and the session reaches OUT only
through a durable surface both sides can read: one GitHub issue per project,
the DROP, where each session posts one comment per read.

This module is that channel and nothing more: the host facts that configure
it, the journal every act lands on, the bundle a session is launched with, the
launch and the delivery, the drop read and its STRICT parse, the cure patch,
and the state each session is inferred to be in. What a read COUNTS AS is
helm/remote_policy.py; which rows it serves and what it records on them is
helm/remote_relay.py.

THE DROP IS UNTRUSTED DATA. A comment body is parsed, never executed: no text
from it reaches a shell, an argv position that could be read as a flag, or a
message sent back to a session. A patch in it is only ever applied with
`git am` onto a scratch clone at the exact reviewed tip, re-authored, and
checked before its commit reaches the project's repository.

THE STATES ARE INFERRED. Nothing on this box can see a remote session, so its
state is folded from the journal and the drop: LAUNCHED (created, first read
under way), AWAITING (a follow-up was delivered and is not answered),
ANSWERED (a report for its current label and tip is on the drop), NUDGED
(silence past the window, and a nudge was sent), SILENT-IDLE (the account's
credit stopped moving and no report is on the drop), UNDELIVERED (nudged to
the cap, the account flat since that nudge, and still no report: completion is
unknown, so recover its reply without holding a launch slot), ARCHIVED (a send
was refused as archived: the CLI's only state signal), and UNKNOWN whenever
the evidence does not decide.

THE IDLE SIGNAL IS THE ACCOUNT'S, NOT THE SESSION'S. The usage endpoint meters
an account, so a flat reading says that NOTHING on the account is spending,
which is evidence about every session on it; a moving reading is evidence
about none of them in particular. So flat can make a session SILENT-IDLE, and
moving never makes one busy.
"""
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time

from . import home, pk, remote_credit, remote_policy, trailer_rung, vcs

CONFIG = "remote-sessions.json"
DRIVER_CLAUDE_CLOUD = "claude-cloud"
DRIVERS = (DRIVER_CLAUDE_CLOUD,)

LAUNCHED = "LAUNCHED"
AWAITING = "AWAITING"
ANSWERED = "ANSWERED"
NUDGED = "NUDGED"
SILENT_IDLE = "SILENT-IDLE"
UNDELIVERED = "UNDELIVERED"
ARCHIVED = "ARCHIVED"
UNKNOWN = "UNKNOWN"
STATES = (LAUNCHED, AWAITING, ANSWERED, NUDGED, SILENT_IDLE, UNDELIVERED,
          ARCHIVED, UNKNOWN)
#: The states that hold a concurrency slot. UNDELIVERED may still answer, but
#: the bounded recovery state holds no slot and receives no more nudges.
LIVE = (LAUNCHED, AWAITING, NUDGED, SILENT_IDLE)

#: A FULL object id: 40 hex (sha1) or 64 hex (sha256), and nothing between.
#: The span 40 to 64 also admitted 41 to 63, which names no object
#: (task/3437). Every full-id check in this module reads this one pattern.
_FULL_ID = re.compile(r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")

SWITCH_ENV = "HELM_REMOTE_SESSIONS"
NUDGE_AFTER_ENV = "HELM_REMOTE_NUDGE_AFTER_S"
MAX_NUDGES_ENV = "HELM_REMOTE_MAX_NUDGES"
LAUNCH_TIMEOUT_ENV = "HELM_REMOTE_LAUNCH_TIMEOUT_S"
REFRESH_MODEL_ENV = "HELM_REMOTE_REFRESH_MODEL"
CLAUDE_ENV = "HELM_REMOTE_CLAUDE"
GH_ENV = "HELM_REMOTE_GH"
IDLE_FLAT_ENV = "HELM_REMOTE_IDLE_FLAT_S"
PERMISSION_ENV = "HELM_REMOTE_PERMISSION_MODE"
#: The modes `claude --permission-mode` accepts. A session nobody watches must
#: not wait on an approval, so the default is the one that never asks; `none`
#: omits the flag and leaves the account's own default.
PERMISSION_MODES = ("acceptEdits", "auto", "bypassPermissions", "manual",
                    "dontAsk", "plan")
DEFAULT_PERMISSION_MODE = "bypassPermissions"
#: The levels `claude --effort` accepts; a seat's `effort` names one.
EFFORTS = ("low", "medium", "high", "xhigh", "max")

#: HOW A SEAT'S TASK REACHES THE CLOUD (task/3517, MEASURED 2026-09-28): a
#: session created with `claude --cloud` now comes up as a bundle session with
#: no GitHub access, whatever branch tracking the launch sets, while a session
#: the owner creates in the web UI with the repository selected can fetch,
#: push and comment. So the default is a STANDING session per account, named
#: in the host facts and fed with `deliver`; the CLI launch is kept behind the
#: seat's explicit `"transport": "cli"`, and a standing seat whose accounts
#: name no usable standing session is REFUSED, never launched.
SEAT_STANDING = "standing"
SEAT_CLI = "cli"
SEAT_TRANSPORTS = (SEAT_STANDING, SEAT_CLI)
_SID = re.compile(r"session_[A-Za-z0-9]{8,64}\Z")

#: Linux refuses one argv string past 128 KiB; a follow-up rides argv.
MAX_MESSAGE_BYTES = 120000
#: A comment body past this is not a review report.
MAX_REPORT_BYTES = 262144
#: Consecutive failed sends with no archive signal before the state is UNKNOWN.
SEND_FAILURES_UNKNOWN = 3


def _int_env(name, default):
    try:
        value = int(os.environ.get(name) or default)
    except ValueError:
        return default
    return value if value > 0 else default


def enabled():
    return str(os.environ.get(SWITCH_ENV) or "on").strip().casefold() \
        not in ("0", "off", "no", "false")


def nudge_after_s():
    return _int_env(NUDGE_AFTER_ENV, 5400)


def max_nudges():
    return _int_env(MAX_NUDGES_ENV, 2)


def idle_flat_s():
    return _int_env(IDLE_FLAT_ENV, 1800)


def permission_mode():
    """(mode or None, why). None with no why means the flag is omitted."""
    raw = str(os.environ.get(PERMISSION_ENV) or DEFAULT_PERMISSION_MODE).strip()
    if raw.casefold() == "none":
        return None, None
    if raw not in PERMISSION_MODES:
        return None, "%s=%r is not one of %s or none" % (
            PERMISSION_ENV, raw, ", ".join(PERMISSION_MODES))
    return raw, None


def claude_bin():
    return os.environ.get(CLAUDE_ENV) or shutil.which("claude") or "claude"


def gh_bin():
    return os.environ.get(GH_ENV) or shutil.which("gh") or "gh"


# ---------------------------------------------------------------------------
# the one subprocess seam for everything that is not git
# ---------------------------------------------------------------------------

def _run(argv, cwd=None, env=None, timeout=None):
    """(rc, stdout, stderr) as text. rc -1 is spawn trouble or a timeout, and
    a timeout ends the whole process group: `script` runs its command in a
    child, and killing `script` alone leaves that child running."""
    try:
        p = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                             start_new_session=True)
    except OSError as exc:
        return -1, "", "%s: %s" % (type(exc).__name__, exc)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except OSError:
            pass
        out, err = p.communicate()
        return -1, out.decode("utf-8", "replace"), "timed out after %ss" % timeout
    return p.returncode, out.decode("utf-8", "replace"), \
        err.decode("utf-8", "replace")


#: Tests replace this; every claude, gh, script and systemctl call reads it.
RUN = _run


def _git(cwd, *args, env=None, stdin=None, timeout=120):
    """(rc, out, err) through helm's one git seam (helm/vcs.py)."""
    return vcs.backend(cwd).text(cwd, *args, env=env, stdin=stdin,
                                 timeout=timeout)


# ---------------------------------------------------------------------------
# host facts: which remote seats exist, which accounts pay, which projects
# ---------------------------------------------------------------------------

def load_config():
    """(config, why). The file is `<helm home>/_global/remote-sessions.json`,
    read on every call. Absent configures nothing; a file that does not read
    is `why`, never read as absent."""
    path, obj, why = home.global_json(CONFIG)
    if why:
        return None, why
    obj = obj or {}
    seats = obj.get("seats") or {}
    projects = obj.get("projects") or {}
    if not isinstance(seats, dict) or not isinstance(projects, dict):
        return None, "%s: `seats` and `projects` must be JSON objects" % path
    return {"path": path,
            "seats": {str(k).casefold(): v for k, v in seats.items()},
            "projects": projects}, None


def seat_problem(seat):
    """Why a configured seat cannot be driven, or None."""
    if not isinstance(seat, dict):
        return "the seat entry is not a JSON object"
    if seat.get("driver") not in DRIVERS:
        return "driver must be one of %s" % ", ".join(DRIVERS)
    refusal = remote_policy.reviewer_refusal(seat.get("model"))
    if refusal:
        return refusal
    effort = seat.get("effort")
    if effort is not None and effort not in EFFORTS:
        return "effort must be one of %s" % ", ".join(EFFORTS)
    if seat.get("transport", SEAT_STANDING) not in SEAT_TRANSPORTS:
        return "transport must be one of %s" % ", ".join(SEAT_TRANSPORTS)
    review_seat = seat.get("review_seat")
    if review_seat is not None and not (
            isinstance(review_seat, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", review_seat)):
        return "review_seat must name a seat"
    accounts = seat.get("accounts")
    if not isinstance(accounts, list) or not accounts or not all(
            isinstance(a, dict) and a.get("email") and a.get("home")
            for a in accounts):
        return "accounts must be a non-empty list of {email, home}"
    for a in accounts:
        sid = a.get("standing_session")
        if sid is not None and not (isinstance(sid, str) and _SID.match(sid)):
            return "%s: standing_session must be a session id (session_...)" \
                % a["email"]
        bound = a.get("standing_repo")
        if bound is not None and not (isinstance(bound, str)
                                      and _REPO.match(bound)):
            return "%s: standing_repo must be owner/repo" % a["email"]
        # THE ORCA-SYNCED DEFAULT HOME: writing its trust flag or refreshing
        # its token races the live seats that share it, so it pays only when
        # the account says so in so many words.
        if _is_default_home(a["home"]) and a.get("default_home") is not True:
            return "%s: %s is the default home live seats share; set " \
                "\"default_home\": true on the account to use it" % (
                    a["email"], a["home"])
    return None


def _is_default_home(path):
    default = os.path.realpath(os.path.expanduser("~/.claude"))
    return os.path.realpath(os.path.expanduser(str(path))) == default


def seat_transport(seat):
    """`standing` (the default) or `cli`, the explicit opt-in."""
    return (seat or {}).get("transport") or SEAT_STANDING


def seat_of(name, cfg=None):
    """(seat dict, why) for a configured remote seat, (None, None) when the
    name is not one."""
    if cfg is None:
        cfg, why = load_config()
        if why:
            return None, why
    seat = cfg["seats"].get(str(name or "").casefold())
    if seat is None:
        return None, None
    problem = seat_problem(seat)
    return (None, problem) if problem else (dict(seat), None)


def accounts_of(seat):
    """Each account as {email, home, standing, bound}: `bound` is the
    `standing_repo` its standing session was created with in the web UI."""
    return [{"email": a["email"],
             "home": os.path.realpath(os.path.expanduser(a["home"])),
             "standing": a.get("standing_session"),
             "bound": a.get("standing_repo")}
            for a in seat.get("accounts") or ()]


def bound_refusal(account, push_repo):
    """None when the account's standing session was created on `push_repo`,
    else why. A standing session pushes to the repository the web UI
    attached, which the relay's PRIVATE check never sees, so a session not
    declared bound to the push repository is never handed a task."""
    bound = account.get("bound")
    if bound and push_repo and bound.casefold() == push_repo.casefold():
        return None
    return ("standing session %s %s, and this project pushes to %s: set "
            "\"standing_repo\" on the account to the repository the session "
            "was created with, or create one on %s" % (
                account.get("standing"),
                "is bound to %s" % bound if bound else "names no standing_repo",
                push_repo, push_repo))


def archived_standing(journal):
    """{session id} of every standing session a send found archived: the
    relay never delivers to one again."""
    return {e.get("sid") for e in journal or ()
            if e.get("event") == "standing-archived" and e.get("sid")}


def seat_door(recipient):
    """The dispatch door's answer for a remote seat, or None when `recipient`
    is not one: (ok, refusal, note).

    A remote seat has no roster row and no pane, so the roster rung and the
    pane census would refuse it or call it gone. Its usability is the relay's
    instead: the switch, the seat entry, and the credit. REFUSE ONLY ON A
    MEASURED CONTRADICTION, as the pane rung does: every account read at or
    below the floor refuses; an account nobody has read yet admits, and says
    so."""
    try:
        cfg, why = load_config()
    except Exception:                      # noqa: BLE001 — never lose a send
        return None
    if cfg is None or str(recipient or "").casefold() not in cfg["seats"]:
        return None
    if not enabled():
        return False, ("@%s is a remote seat and remote sessions are switched "
                       "off (%s)" % (recipient, SWITCH_ENV)), None
    seat, why = seat_of(recipient, cfg)
    if why:
        return False, "@%s is a remote seat that cannot be driven: %s" % (
            recipient, why), None
    readings = [e for e in read_journal() if e.get("event") == "credit"]
    floor = remote_credit.floor_usd()
    known = [remote_credit.remembered(readings, a["email"])
             for a in accounts_of(seat)]
    if known and all(r is not None and (r.get("left") or 0) <= floor
                     for r in known):
        return False, ("@%s is a remote seat and every account it may bill is "
                       "at or below the $%.2f credit floor" % (recipient, floor)
                       ), None
    note = ("@%s is a remote seat: `helm remote tick` launches or continues a "
            "%s session for this row and records its read" % (
                recipient, seat.get("driver")))
    if any(r is None for r in known):
        note += "; credit is UNKNOWN until the relay reads it"
    return True, None, note


def project_for(cfg, repo_id):
    """(name, project dict) whose repository shares `repo_id` (the row's git
    common dir), or (None, None)."""
    from . import dispatches
    want = os.path.realpath(str(repo_id or ""))
    for name, project in (cfg or {}).get("projects", {}).items():
        if not isinstance(project, dict) or not project.get("repo"):
            continue
        repo = os.path.realpath(os.path.expanduser(project["repo"]))
        info = dispatches._repo_info(repo)
        if info and info["repo_id"] == want:
            return name, dict(project, repo=repo)
    return None, None


_DROP = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.-]{0,99})/([A-Za-z0-9_.-]{1,100})"
                   r"#([1-9][0-9]{0,9})\Z")


def parse_drop(spec):
    """(owner, repo, number) from `owner/repo#N`, else None."""
    m = _DROP.match(str(spec or "").strip())
    return (m.group(1), m.group(2), int(m.group(3))) if m else None


# ---------------------------------------------------------------------------
# the journal: every launch, delivery, report, record and reading
# ---------------------------------------------------------------------------

def state_dir():
    return os.path.join(home.global_dir(), ".state", "remote-sessions")


def journal_path():
    return os.path.join(state_dir(), "journal.jsonl")


def work_dir():
    return os.path.join(state_dir(), "work")


def append(event):
    """Append one event; False when it could not be written. One line under
    PIPE_BUF with O_APPEND, so concurrent writers never interleave a line."""
    row = dict(event)
    row.setdefault("ts", pk.now_ts())
    try:
        os.makedirs(state_dir(), exist_ok=True)
        with open(journal_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        return True
    except OSError:
        return False


def read_journal(strict=False):
    """Every event, oldest first. A garbled line is skipped, never fatal. An
    absent journal is no events. With `strict`, a journal that exists and
    cannot be read raises instead of reading as no events, for a caller that
    must not take an unread journal for an empty one."""
    out = []
    try:
        with pk.open_regular(journal_path(), encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    out.append(row)
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        if strict:
            raise
    return out


# ---------------------------------------------------------------------------
# the bundle: a repository with no remote, holding exactly the tip and base
# ---------------------------------------------------------------------------

def build_bundle(repo, tip, base_ref, dest, author=None):
    """({dest, tip, base}, None) or (None, why).

    `git init`, then fetch EXACTLY the reviewed tip (as branch `review`) and
    its merge-base with the project's base ref (as `base`) from the project
    repository, and nothing else. There is NO remote, so when Claude Code
    uploads it (CCR_FORCE_BUNDLE=1) the session has nothing it could push to.
    The check at the end is the guarantee, not the recipe: a bundle that
    reads as having a remote is refused."""
    rc, base, err = _git(repo, "merge-base", base_ref, tip)
    if rc != 0 or not _FULL_ID.fullmatch(base or ""):
        return None, "no merge-base of %s and %s in %s: %s" % (
            base_ref, tip[:12], repo, err or base)
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest, mode=0o700)
    steps = [("init", "-q")]
    if author:
        name, email = author
        steps += [("config", "user.name", name), ("config", "user.email", email)]
    steps += [("fetch", "--no-tags", "-q", repo,
               "%s:refs/heads/review" % tip, "%s:refs/heads/base" % base),
              ("checkout", "-q", "review")]
    for args in steps:
        rc, _out, err = _git(dest, *args)
        if rc != 0:
            return None, "bundle step `git %s` failed: %s" % (args[0], err)
    rc, remotes, _err = _git(dest, "remote")
    if rc != 0 or remotes.strip():
        return None, "the bundle has a remote (%s); refusing to launch it" % (
            remotes.strip() or "unreadable")
    return {"dest": dest, "tip": tip, "base": base}, None


def mark_trusted(claude_home, path):
    """Mark `path` trusted in that home's .claude.json, or return why not.
    `--cloud` stops at the folder-trust prompt otherwise. The write is atomic
    and keeps the file's owner-only mode."""
    cfg = os.path.join(claude_home, ".claude.json")
    data = pk.read_json(cfg, None)
    if not isinstance(data, dict):
        return "%s is missing or not a JSON object" % cfg
    projects = data.setdefault("projects", {})
    if not isinstance(projects, dict):
        return "%s: projects is not a JSON object" % cfg
    entry = projects.setdefault(path, {})
    if entry.get("hasTrustDialogAccepted") is True:
        return None
    entry["hasTrustDialogAccepted"] = True
    try:
        pk.atomic_write(cfg, json.dumps(data, indent=2) + "\n", mode=0o600)
    except OSError as exc:
        return "could not write %s: %s" % (cfg, exc)
    return None


# ---------------------------------------------------------------------------
# the pushed-branch transport (the default)
# ---------------------------------------------------------------------------
#
# WHY A BRANCH AND NOT A BUNDLE (MEASURED): sessions launched from an uploaded
# bundle never posted to the drop, even when asked twice, while a session
# launched from a checkout whose origin was a GitHub repository posted without
# trouble. A bundle session gets no GitHub credentials. So the relay pushes the
# exact reviewed tip to the project's PRIVATE drop repository as a namespaced
# branch and launches from a scratch clone whose origin is that repository:
# the session posts with the auth that checkout gives it, and returns a cure
# as a branch instead of a patch pasted into a comment.
#
# THE PUSH REFUSES ANY REPOSITORY THAT IS NOT PRIVATE, read from GitHub before
# every push and failing closed: an unreadable answer is a refusal, because a
# push to a public repository publishes the lane.

TRANSPORT_BRANCH = "branch"
TRANSPORT_BUNDLE = "bundle"
TRANSPORTS = (TRANSPORT_BRANCH, TRANSPORT_BUNDLE)
DEFAULT_BRANCH_PREFIX = "cloudrev/"
_PREFIX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,39}\Z")
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9_.-]{1,100}\Z")
_PUSH_URL = re.compile(
    r"(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"([A-Za-z0-9][A-Za-z0-9_.-]{0,99})/"
    r"([A-Za-z0-9_.-]{1,100}?)(?:\.git)?/?\Z", re.I)


def branch_names(branch):
    """The four branches one read uses, named from its own branch."""
    return {"branch": branch, "base": branch + "-base",
            "cure": branch + "-cure", "report": branch + "-report"}


def _push_repo_of(url):
    """The canonical GitHub `owner/repo` a push URL names, else None.

    Kept as the one narrow seam tests replace for a local bare repository. No
    config or environment switch admits a non-GitHub production destination.
    """
    m = _PUSH_URL.fullmatch(str(url or "").strip())
    return "%s/%s" % m.groups() if m else None


def _push_url_refusal(push_repo, push_url):
    actual = _push_repo_of(push_url)
    if not actual:
        return "push_url is not a canonical GitHub repository URL for %s" % \
            push_repo
    if actual.casefold() != push_repo.casefold():
        return "push_url names %s, not %s" % (actual, push_repo)
    return None


def transport_of(project):
    """(transport, push repo `owner/repo`, push url, branch prefix, why).

    `transport` is the project's `transport` (default `branch`). The push
    repository is `push_repo`, else the drop's own repository; the URL is
    `push_url`, else its https address; the prefix is `branch_prefix`, else
    `cloudrev/`. A value that fails its shape is `why`, never a default."""
    transport = project.get("transport") or TRANSPORT_BRANCH
    if transport not in TRANSPORTS:
        return None, None, None, None, "transport must be one of %s" % (
            ", ".join(TRANSPORTS))
    drop = parse_drop(project.get("drop"))
    push_repo = project.get("push_repo") or (
        "%s/%s" % drop[:2] if drop else None)
    if transport == TRANSPORT_BUNDLE:
        return transport, None, None, None, None
    if not push_repo or not _REPO.match(push_repo):
        return None, None, None, None, "push_repo must be owner/repo"
    prefix = project.get("branch_prefix") or DEFAULT_BRANCH_PREFIX
    if not _PREFIX.match(prefix) or ".." in prefix:
        return None, None, None, None, "branch_prefix %r is not a branch " \
            "prefix" % prefix
    url = project.get("push_url") or "https://github.com/%s.git" % push_repo
    if not isinstance(url, str):
        return None, None, None, None, "push_url must be a GitHub repository URL"
    url = os.path.expanduser(url)
    why = _push_url_refusal(push_repo, url)
    return (None, None, None, None, why) if why else (
        transport, push_repo, url, prefix, None)


def private_refusal(push_repo, push_url=None):
    """None only when URL and GitHub prove the same PRIVATE repository.

    `push_url`, when supplied, is the resolved push destination Git will use,
    not merely the configured spelling. Anything unparseable, mismatched,
    PUBLIC, INTERNAL or unreadable refuses before a ref is written.
    """
    why = _push_url_refusal(push_repo, push_url) if push_url else None
    if why:
        return why
    rc, out, err = RUN([gh_bin(), "repo", "view", push_repo, "--json",
                        "visibility,nameWithOwner"], timeout=60)
    answer = next((v for v in _json_values(out) if isinstance(v, dict)), {})
    visibility = answer.get("visibility")
    canonical = answer.get("nameWithOwner")
    same = isinstance(canonical, str) and \
        canonical.casefold() == push_repo.casefold()
    if rc == 0 and visibility == "PRIVATE" and same:
        return None
    detail = "says it is %s" % visibility if visibility else \
        "did not answer its visibility (rc %s: %s)" % (
            rc, (err or out).strip()[:120])
    if visibility == "PRIVATE" and not same:
        detail = "identified it as %r" % canonical
    return ("refusing to push to %s: GitHub %s, and only that PRIVATE "
            "repository may carry a lane under review" % (push_repo, detail))


def _origin_push_url(workdir):
    rc, out, err = _git(workdir, "remote", "get-url", "--push", "--all",
                        "origin")
    urls = out.splitlines() if rc == 0 else []
    return (urls[0], None) if len(urls) == 1 and urls[0] else (
        None, "the checkout must have exactly one readable origin push URL: %s" %
        (err or ("found %d" % len(urls))).strip()[:160])


def _claim_push_args(names, tip, base):
    """One atomic create-only push claims the read's whole namespace."""
    refs = {names["branch"]: tip, names["base"]: base,
            names["cure"]: tip, names["report"]: tip}
    leases = ["--force-with-lease=refs/heads/%s:" % name for name in refs]
    specs = ["%s:refs/heads/%s" % (sha, name)
             for name, sha in refs.items()]
    return refs, leases + ["origin"] + specs


def build_branch_checkout(repo, tip, base_ref, dest, author, push_repo, url,
                          branch):
    """({dest, tip, base, branch, owned_refs}, None) or (None, why).

    The actual push URL must canonically name the repository whose PRIVATE
    visibility is checked. One atomic, create-only push claims all four refs:
    reviewed tip, base, cure and report. Cure/report begin at the reviewed tip,
    so the remote session can advance them normally; any pre-existing ref makes
    the whole launch refuse without overwriting it.
    """
    names = branch_names(branch)
    rc, base, err = _git(repo, "merge-base", base_ref, tip)
    if rc != 0 or not _FULL_ID.fullmatch(base or ""):
        return None, "no merge-base of %s and %s in %s: %s" % (
            base_ref, tip[:12], repo, err or base)
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest, mode=0o700)
    steps = [("init", "-q")]
    if author:
        steps += [("config", "user.name", author[0]),
                  ("config", "user.email", author[1])]
    steps += [("remote", "add", "origin", url),
              ("fetch", "--no-tags", "-q", repo,
               "%s:refs/heads/%s" % (tip, names["branch"]),
               "%s:refs/heads/%s" % (base, names["base"])),
              ("checkout", "-q", names["branch"]),
              ("config", "branch.%s.remote" % names["branch"], "origin"),
              ("config", "branch.%s.merge" % names["branch"],
               "refs/heads/" + names["branch"])]
    for args in steps:
        rc, _out, err = _git(dest, *args)
        if rc != 0:
            return None, "checkout step `git %s` failed: %s" % (args[0], err)
    actual, why = _origin_push_url(dest)
    why = why or private_refusal(push_repo, actual)
    if why:
        return None, why
    owned, args = _claim_push_args(names, tip, base)
    rc, _out, err = _git(dest, "push", "-q", "--atomic", *args, timeout=600)
    if rc != 0:
        return None, "claiming branch namespace %s failed: %s" % (
            names["branch"], (err or "").strip()[:200])
    return {"dest": dest, "tip": tip, "base": base, "branch": branch,
            "owned_refs": owned}, None


def push_reread(workdir, repo, tip, base, branch, push_repo):
    """Create and push one re-read namespace, or refuse without overwrite.

    Returns (owned refs, None), or (None, why). The origin's resolved push URL
    is rebound to the PRIVATE repository immediately before the push.
    """
    url, why = _origin_push_url(workdir)
    why = why or private_refusal(push_repo, url)
    if why:
        return None, why
    names = branch_names(branch)
    rc, _out, err = _git(workdir, "fetch", "--no-tags", "-q", repo,
                         "%s:refs/heads/%s" % (tip, names["branch"]),
                         "%s:refs/heads/%s" % (base, names["base"]))
    if rc != 0:
        return None, "fetching the re-read into %s failed: %s" % (workdir, err)
    owned, args = _claim_push_args(names, tip, base)
    rc, _out, err = _git(workdir, "push", "-q", "--atomic", *args, timeout=600)
    return (owned, None) if rc == 0 else (None,
        "claiming re-read namespace %s failed: %s" % (
            names["branch"], (err or "").strip()[:200]))


def remote_branch(workdir, name):
    """(commit, None) for `name` on the checkout's origin, (None, None) when
    the branch is absent, or (None, why) when the read failed."""
    rc, out, err = _git(workdir, "ls-remote", "--heads", "origin",
                        "refs/heads/" + name, timeout=120)
    if rc != 0:
        return None, "reading %s on the drop repository failed: %s" % (
            name, (err or "").strip()[:200])
    rows = out.splitlines()
    if not rows:
        return None, None
    fields = rows[0].split()
    if len(rows) != 1 or len(fields) != 2 or fields[1] != "refs/heads/" + name \
            or not _FULL_ID.fullmatch(fields[0]):
        return None, "the drop repository gave an unreadable branch answer"
    return fields[0], None


def fetch_branch(workdir, name):
    """(commit, None), (None, None) when the branch is absent, or (None, why).
    The branch lands under refs/remote-read/ in the session's checkout."""
    sha, why = remote_branch(workdir, name)
    if why or not sha:
        return None, why
    rc, _out, err = _git(workdir, "fetch", "--no-tags", "-q", "origin",
                         "+refs/heads/%s:refs/remote-read/%s" % (name, name),
                         timeout=300)
    if rc != 0:
        return None, "fetching %s failed: %s" % (name, (err or "")[:200])
    return sha, None


def branch_report(workdir, branch, seed=None):
    """(text, observed sha, why) for a fallback report branch.

    The launch-owned seed at the reviewed tip means "no report yet". A real
    report's exact observed sha rides the journal so cleanup can spend only
    that observation as its delete lease.
    """
    name = branch_names(branch)["report"]
    sha, why = fetch_branch(workdir, name)
    if why or not sha or sha == seed:
        return None, None, why
    rc, out, _err = vcs.backend(workdir).run(
        workdir, "show", "%s:REVIEW_REPORT.md" % sha, timeout=60)
    if rc != 0 or len(out) > MAX_REPORT_BYTES:
        return None, sha, "the report branch holds no readable REVIEW_REPORT.md"
    return out.decode("utf-8", "replace"), sha, None


def branch_cure(workdir, tip, branch):
    """(mbox, observed sha, why) for the cure branch.

    The launch-owned seed at `tip` means no cure. A cure must descend from the
    reviewed tip; its exact observed sha is later the cleanup lease.
    """
    name = branch_names(branch)["cure"]
    sha, why = fetch_branch(workdir, name)
    if why or not sha or sha == tip:
        return None, None, why
    rc, _out, _err = _git(workdir, "merge-base", "--is-ancestor", tip, sha)
    if rc != 0:
        return None, sha, "the cure branch %s does not descend from %s" % (
            name, tip[:12])
    # --binary, so a binary change reaches verify_cure and is refused BY NAME
    # there, rather than failing to apply for a reason nobody reads.
    rc, out, err = _git(workdir, "format-patch", "--stdout", "--binary",
                        "--no-signature", "%s..%s" % (tip, sha), timeout=120)
    if rc != 0 or not out.strip():
        return None, sha, "the cure branch %s holds no patch: %s" % (name, err)
    return out + "\n", sha, None


def delete_branches(workdir, expected, push_repo):
    """Compare-and-delete only refs still at their owned/observed exact shas.

    `expected` is {branch: sha} from the atomic launch claim, superseded only
    by an exact sha the relay later observed. An absent branch is already gone;
    a moved branch is preserved. Git's remote force-with-lease closes the race
    between this function's read and delete.
    """
    url, why = _origin_push_url(workdir)
    why = why or _push_url_refusal(push_repo, url)
    if why:
        return {name: why for name in expected}
    out = {}
    for name, want in expected.items():
        if not _FULL_ID.fullmatch(str(want or "")):
            out[name] = "no exact owned sha for the branch"
            continue
        current, why = remote_branch(workdir, name)
        if why or not current:
            out[name] = why
            continue
        if current != want:
            out[name] = "branch moved after the relay observed it"
            continue
        lease = "--force-with-lease=refs/heads/%s:%s" % (name, want)
        rc, _o, err = _git(workdir, "push", "-q", lease, "origin",
                           ":refs/heads/" + name, timeout=300)
        out[name] = None if rc == 0 else (err or "delete lease refused").strip()[:200]
    return out


# ---------------------------------------------------------------------------
# the build lane's branches (task/3517)
# ---------------------------------------------------------------------------
#
# A cloud BUILD starts on `<prefix>build-<label>` (the row's tip, normally
# trunk) and hands back by pushing `<prefix>build-<label>-build`. The relay
# claims only the start branch; the build branch is the session's, so one that
# already exists is somebody's work and refuses the launch. The hand-back is
# fetched into a LOCAL lane branch, and nothing is pushed anywhere but the
# PRIVATE push repository.

def build_names(prefix, label):
    """The two branches one build uses, named from its label."""
    start = "%sbuild-%s" % (prefix, label)
    return {"start": start, "build": start + "-build"}


def build_checkout(repo, tip, dest, author, push_repo, url, names):
    """({dest, tip, base, branch, owned_refs}, None) or (None, why).

    An ORDINARY scratch repository (git init and a fetch, so every object is
    copied: Claude Code refuses a repository that borrows objects through an
    alternates file, so never `git clone --shared`) whose origin must
    canonically name the PRIVATE push repository. The start branch is claimed
    create-only at `tip`; a start or build branch already on the push
    repository refuses without touching it."""
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest, mode=0o700)
    start = names["start"]
    steps = [("init", "-q")]
    if author:
        steps += [("config", "user.name", author[0]),
                  ("config", "user.email", author[1])]
    steps += [("remote", "add", "origin", url),
              ("fetch", "--no-tags", "-q", repo,
               "%s:refs/heads/%s" % (tip, start)),
              ("checkout", "-q", start),
              ("config", "branch.%s.remote" % start, "origin"),
              ("config", "branch.%s.merge" % start, "refs/heads/" + start)]
    for args in steps:
        rc, _out, err = _git(dest, *args)
        if rc != 0:
            return None, "checkout step `git %s` failed: %s" % (args[0], err)
    if os.path.exists(os.path.join(dest, ".git", "objects", "info",
                                   "alternates")):
        return None, "the scratch clone borrows objects (alternates); " \
            "Claude Code refuses it"
    actual, why = _origin_push_url(dest)
    why = why or private_refusal(push_repo, actual)
    if why:
        return None, why
    for name in (start, names["build"]):
        sha, why = remote_branch(dest, name)
        if why:
            return None, why
        if sha:
            return None, "branch %s is already on %s; refusing to claim it" % (
                name, push_repo)
    rc, _out, err = _git(dest, "push", "-q", "--atomic",
                         "--force-with-lease=refs/heads/%s:" % start, "origin",
                         "%s:refs/heads/%s" % (tip, start), timeout=600)
    if rc != 0:
        return None, "claiming branch %s failed: %s" % (
            start, (err or "").strip()[:200])
    return {"dest": dest, "tip": tip, "base": tip, "branch": start,
            "owned_refs": {start: tip}}, None


def fetch_lane(repo, workdir, sha, label):
    """(ref, None) or (None, why): the fetched hand-back `sha` (already under
    refs/remote-read/ in `workdir`, see fetch_branch) into the project
    repository as `refs/heads/lane/<label>`, or, where the shared checkout's
    ref guard refuses a branch born outside a room, `refs/remote-build/<label>`.
    A fetch, never a push: nothing leaves this machine. A lane that already
    exists only moves forward (a cure round); a rewrite is refused."""
    rc, out, _err = _git(workdir, "for-each-ref", "--points-at", sha,
                         "--format=%(refname)", "refs/remote-read/")
    source = next((r for r in out.split() if r), None) if rc == 0 else None
    if not source:
        return None, "no fetched branch in %s holds %s" % (workdir, sha[:12])
    candidates = ("refs/heads/lane/" + label, "refs/remote-build/" + label)
    existing = [ref for ref in candidates
                if _git(repo, "rev-parse", "-q", "--verify", ref)[0] == 0]
    for ref in existing[:1] or candidates:
        rc, _out, err = _git(repo, "fetch", "--no-tags", "-q", workdir,
                             "%s:%s" % (source, ref), timeout=300)
        if rc == 0:
            rc, now, _err = _git(repo, "rev-parse", ref)
            if rc == 0 and now.strip() == sha:
                return ref, None
            return None, "%s does not hold %s after the fetch" % (ref, sha[:12])
        if "REFUSED" in (err or "") and not existing:
            continue
        return None, "fetching the build into %s failed: %s" % (
            ref, (err or "").strip()[:200])
    return None, "the ref guard refused both namespaces for the lane"


# ---------------------------------------------------------------------------
# launch and deliver
# ---------------------------------------------------------------------------

def _claude_env(claude_home, extra=None):
    """The environment a claude run gets: the ambient one with every CLAUDE*
    and ANTHROPIC* variable removed (so no API key, base URL or foreign session
    reaches it), then the home that decides which account pays."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLAUDE", "ANTHROPIC"))}
    env["CLAUDE_CONFIG_DIR"] = claude_home
    env.update(extra or {})
    return env


def refresh_token(claude_home, timeout=180):
    """None when the home's access token is live, or was made live; else why.

    `--cloud` does not refresh an expired token (MEASURED: HTTP 401 on create),
    so a minimal non-interactive run in the same home makes Claude Code refresh
    it the way it always does. That run never reviews anything; its model is
    the cheapest one (HELM_REMOTE_REFRESH_MODEL)."""
    if remote_credit.token_live(claude_home):
        return None
    model = os.environ.get(REFRESH_MODEL_ENV) or "haiku"
    rc, _out, err = RUN([claude_bin(), "-p", "reply with the word ok",
                         "--model", model], env=_claude_env(claude_home),
                        timeout=timeout)
    if remote_credit.token_live(claude_home):
        return None
    return "the access token in %s is not live after a refresh run (rc %s: %s)" \
        % (claude_home, rc, (err or "").strip()[:160])


_ANSI = re.compile(r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
_SESSION_ID = re.compile(r"\b(session_[A-Za-z0-9]{8,64})\b")


def launch(claude_home, model, cwd, task, label, timeout=None, mode=None,
           bundle=True, effort=None):
    """(session id, url, why). One `claude --model M [--permission-mode P]
    [--effort E] --cloud TASK` run in the session's directory, under a pseudo-terminal
    (`--cloud` refuses without one). With `bundle`, CCR_FORCE_BUNDLE=1 makes
    Claude Code upload the remote-less repository; without it, the cloud
    session clones the directory's GitHub origin at its current branch, and
    an ambient CCR_FORCE_BUNDLE is removed so it cannot turn a branch launch
    back into a bundle.

    EVERY FLAG COMES BEFORE `--cloud`: it takes the next token as its value,
    so a flag after it is eaten as the task (MEASURED). Whether the permission
    mode carries into the created session is NOT documented for `--cloud`;
    the relay records the mode it passed so the first live launch settles it.
    The task text rides a file the shell reads inside double quotes, where
    its content is neither split nor expanded again."""
    timeout = timeout or _int_env(LAUNCH_TIMEOUT_ENV, 150)
    tasks = os.path.join(work_dir(), "tasks")
    os.makedirs(tasks, mode=0o700, exist_ok=True)
    task_file = os.path.join(tasks, label + ".task")
    script_log = os.path.join(tasks, label + ".typescript")
    pk.atomic_write(task_file, task, mode=0o600)
    flags = "--model %s" % shlex.quote(model)
    if mode:
        flags += " --permission-mode %s" % shlex.quote(mode)
    if effort:
        flags += " --effort %s" % shlex.quote(effort)
    command = '%s %s --cloud "$(cat %s)"' % (
        shlex.quote(claude_bin()), flags, shlex.quote(task_file))
    env = _claude_env(claude_home, {"CCR_FORCE_BUNDLE": "1"} if bundle else {})
    if not bundle:
        env.pop("CCR_FORCE_BUNDLE", None)
    rc, _out, err = RUN(["script", "-q", "-c", command, script_log], cwd=cwd,
                        env=env, timeout=timeout)
    try:
        with open(script_log, encoding="utf-8", errors="replace") as f:
            text = _ANSI.sub("", f.read())
    except OSError:
        text = ""
    m = _SESSION_ID.search(text)
    if not m:
        return None, None, ("the launch reported no session id (rc %s: %s)"
                            % (rc, (err or text[-200:]).strip()[:200]))
    sid = m.group(1)
    return sid, "https://claude.ai/code/" + sid, None


def deliver(claude_home, sid, message, timeout=180):
    """(ok, archived, why) for one follow-up. `claude -p MSG --cloud SID`
    queues one message and exits; `--output-format json` answers
    {ok, session_id, url} or {ok: false, error}. An archived session refuses:
    the only state signal the CLI gives."""
    if len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
        return False, False, "the message is over %d bytes" % MAX_MESSAGE_BYTES
    if not re.fullmatch(r"session_[A-Za-z0-9]{8,64}", str(sid or "")):
        return False, False, "no addressable session id"
    rc, out, err = RUN([claude_bin(), "-p", message, "--cloud", sid,
                        "--output-format", "json"],
                       env=_claude_env(claude_home), timeout=timeout)
    answer = None
    for value in _json_values(out):
        if isinstance(value, dict) and "ok" in value:
            answer = value
    if answer is None:
        return False, False, "unreadable answer (rc %s: %s)" % (
            rc, (err or out).strip()[:200])
    if answer.get("ok") is True:
        return True, False, None
    error = str(answer.get("error") or "refused")[:300]
    archived = bool(re.search(r"archiv|not found|no such session", error, re.I))
    return False, archived, error


def _json_values(text):
    """Every JSON value in `text`, in order: `gh api --paginate` prints one
    array per page back to back, and a CLI may print a banner first."""
    decoder = json.JSONDecoder()
    out, i, text = [], 0, text or ""
    while i < len(text):
        j = min([k for k in (text.find("{", i), text.find("[", i)) if k >= 0]
                or [len(text)])
        if j >= len(text):
            break
        try:
            value, end = decoder.raw_decode(text, j)
        except ValueError:
            i = j + 1
            continue
        out.append(value)
        i = end
    return out


# ---------------------------------------------------------------------------
# the messages helm sends IN (fixed text; nothing from the drop is echoed)
# ---------------------------------------------------------------------------

PROTOCOL = """HOW TO WORK AND REPORT (read this first)
{workspace}
- Say which test failures you believe come from your environment alone, and never count those as findings.
- REPORT ONLY. {report_only}
- Be adversarial. Find the input, sequence or state where the change is wrong, and reproduce it. A finding you reproduced with a test or script is MEASURED; one you reasoned from the code without running is INFERRED. Label every finding.
- When you finish, post ONE comment with `gh issue comment {issue} -R {drop_repo} --body-file <file>`. Its first line must be exactly:
  CLOUD REVIEW {label} {tip12} model=<your exact model id from your system prompt> account={account}
  then these lines:
  VERDICT: APPROVE or FIX
  FINDING-COUNT: <n>
  then numbered findings, each: [BLOCKING|MINOR] [MEASURED|INFERRED] title / file:line / failure scenario (concrete input -> wrong output) / how you verified it / the cure you propose.
  An APPROVE carries no BLOCKING finding; a FIX carries at least one finding.
{cures}
- The comment is the ONLY way your report reaches anyone: a reply in this session is never read. If `gh issue comment` fails, run `gh auth status`, retry once, and if it still fails, end your reply with the whole report and the exact gh error. {fallback}
- Finish within about an hour of work. Depth on the riskiest paths beats breadth."""

#: The transport-shaped parts of the protocol. BRANCH: the session's workspace
#: is a clone of the project's PRIVATE drop repository, so it has the GitHub
#: auth a bundle session lacks (MEASURED: bundle sessions never posted), and it
#: returns its cure as a branch. BUNDLE: an uploaded repository with no remote.
PROTOCOL_PARTS = {
    "branch": {
        "workspace": "- Your workspace is a clone of a private repository, on branch `{branch}`: the exact tip under review. Its trunk is branch `{base_branch}`: run `git fetch origin {base_branch}`, then review exactly `git diff FETCH_HEAD..HEAD` and `git log FETCH_HEAD..HEAD`.",
        "report_only": "Push nothing except the cure branch below, open no pull request, and edit nothing on GitHub except the one comment below.",
        "cures": "  If you wrote cures, commit them on top of `{branch}` and push them as a NEW branch with `git push origin HEAD:refs/heads/{cure_branch}`. Never push to any other branch. Your commits must carry no Co-Authored-By or other AI trailer lines.",
        "fallback": "Also commit the whole report, exactly as the comment would be, as REVIEW_REPORT.md on a new branch from `{branch}` and push it with `git push origin HEAD:refs/heads/{report_branch}`; never put it on the cure branch.",
    },
    "bundle": {
        "workspace": "- Your workspace repo has two branches: `review` (the tip under review, checked out) and `base` (where the lane forked from its trunk). Review exactly `git diff base..review` and `git log base..review`.",
        "report_only": "Do not push, open pull requests, create branches or edit anything on GitHub except the one comment below. The repo has no remote on purpose.",
        "cures": "  If you wrote cures, commit them locally on `review` and paste `git format-patch {tip12}..HEAD --stdout` (only your own commits, after the review tip) inside a <details><summary>patch</summary> block in the same comment. Your commits must carry no Co-Authored-By or other AI trailer lines.",
        "fallback": "Also commit it as REVIEW_REPORT.md on a new local branch; nothing here can read it, but a person opening the session can.",
    },
}


def task_text(label, tip, count, brief, protocol, drop, account,
              transport=None, branch=None):
    """The launch task: a header naming the label and tip, the author's
    brief, and the project's protocol with its placeholders filled. The
    transport's parts are filled first, so they may carry placeholders too."""
    owner, repo, number = parse_drop(drop)
    filled = protocol or PROTOCOL
    parts = PROTOCOL_PARTS[transport or TRANSPORT_BRANCH]
    names = branch_names(branch or "")
    # Named placeholders only, by plain replacement: a project's protocol file
    # is prose and may carry braces of its own.
    for key, value in [("{%s}" % k, v) for k, v in parts.items()] + [
            ("{label}", label), ("{tip12}", tip[:12]), ("{account}", account),
            ("{issue}", str(number)), ("{drop_repo}", "%s/%s" % (owner, repo)),
            ("{branch}", names["branch"]), ("{base_branch}", names["base"]),
            ("{cure_branch}", names["cure"]),
            ("{report_branch}", names["report"])]:
        filled = filled.replace(key, value)
    return ("Code review. label=%s, tip %s (%s commit(s) over base).\n%s\n\n%s"
            % (label, tip[:12], count, str(brief or "").strip(), filled))


def _expects(model, effort):
    return ("This task expects model %s at effort %s. If you are running a "
            "different model or effort, say so in your report."
            % (model, effort or "(your default)"))


def origin_check(push_repo):
    """The line every standing task carries: the session's origin is the
    repository the web UI attached, which no check here can see, so it
    pushes only after reading that origin names the push repository."""
    return ("Before any push, run `git remote get-url origin`: unless it names "
            "%s, push nothing and say so in your report." % push_repo)


def standing_preface(model, effort, branch, base_branch, push_repo=None):
    """What a STANDING session is told before a review task: it is not on the
    branch under review, so it fetches it, it is told the model and effort
    this seat expects, so a master running something else says so, and it
    pushes nothing unless its origin names `push_repo`."""
    return ("You are a standing session: this is a new task, not a follow-up "
            "to the last one. %s\nFirst run `git fetch origin %s %s` and "
            "`git checkout -B %s origin/%s`; that is the workspace the task "
            "below describes.%s\n\n" % (
                _expects(model, effort), branch, base_branch, branch, branch,
                " " + origin_check(push_repo) if push_repo else ""))


BUILD_PROTOCOL = """HOW TO BUILD AND HAND BACK (read this first)
- Start with `git fetch origin {start}` and `git checkout -B {start} origin/{start}`: that is where this {what} starts.
- Commit as {name} <{email}>: run `git config user.name "{name}"` and `git config user.email {email}` first. No commit message may carry a Co-Authored-By line, a model name, an AI link or a generated-with footer.
- Push ONLY to `{build}`, with `git push origin HEAD:refs/heads/{build}`. Never push any other branch, open no pull request, and edit nothing on GitHub except the one comment below.
- {origin_check}
- Test what you change, and say what you ran.
- When the branch is pushed, post ONE comment with `gh issue comment {issue} -R {drop_repo} --body-file <file>`. Its first lines must be exactly:
  {header} {label}
  Branch: {build}
  Tip: <the full commit id you pushed>
  Model: <your exact model id from your system prompt>
  Effort: <your effort level>
  then what you built and how you tested it.
- The pushed branch IS the hand-back: a report whose branch is not on the repository counts for nothing. A reply in this session is never read."""


def handback_header(round_=0):
    return "BUILD" if not round_ else "CURE" if round_ == 1 else \
        "CURE%d" % round_


def build_author(project):
    """(name, email) every cloud build commit carries: the project's
    `build_author`, else its `cure_author`, else None (a build is refused).
    A host fact, never a literal here: the owner's identity is not source."""
    return parse_author(project.get("build_author")
                        or project.get("cure_author"))


def build_task_text(label, tip, brief, names, drop, account, model, effort,
                    author, round_=0, push_repo=None):
    """The task a cloud BUILD (round 0) or CURE (round n) session gets: a
    header, the model and effort it is expected to run, the author's brief,
    and the build protocol with the commit identity `author` ((name, email),
    see build_author), the one branch it may push and the hand-back header it
    reports under. `account` is the paying account, named so the session can
    see which one it bills."""
    owner, repo, number = parse_drop(drop)
    header = handback_header(round_)
    protocol = BUILD_PROTOCOL
    for key, value in (("{start}", names["start"]), ("{build}", names["build"]),
                       ("{name}", author[0]), ("{email}", author[1]),
                       ("{what}", "cure" if round_ else "build"),
                       ("{issue}", str(number)),
                       ("{drop_repo}", "%s/%s" % (owner, repo)),
                       ("{origin_check}", origin_check(
                           push_repo or "%s/%s" % (owner, repo))),
                       ("{header}", header), ("{label}", label)):
        protocol = protocol.replace(key, value)
    return ("%s task. label=%s, start %s on branch %s; billed to %s.\n%s\n\n"
            "%s\n\n%s" % (header, label, tip[:12], names["start"], account,
                           _expects(model, effort), str(brief or "").strip(),
                           protocol))


def build_nudge_message(label, names, round_=0):
    return ("REMINDER label=%s: nothing is on `%s` yet. If you are still "
            "working, keep going. When you are done, push it with `git push "
            "origin HEAD:refs/heads/%s` and post the ONE comment the task "
            "names, first line `%s %s`." % (label, names["build"],
                                           names["build"],
                                           handback_header(round_), label))


def build_correction_message(label, names, why, round_=0):
    """`why` is this module's own reason, never comment text."""
    return ("CORRECTION label=%s: your hand-back did not count (%s). Push the "
            "work with `git push origin HEAD:refs/heads/%s`, then post one new "
            "comment whose first lines are `%s %s`, `Branch: %s` and `Tip: "
            "<the full commit id>`." % (label, why, names["build"],
                                        handback_header(round_), label,
                                        names["build"]))


def reread_message(label, old_tip, new_tip, patch):
    """The follow-up for a re-read at a new tip. The session cannot fetch, so
    the delta travels as `git format-patch old..new` for it to `git am` onto
    the tip it already holds. Its commit ids will differ from ours, so the
    report names OUR new tip, which this message spells out."""
    return ("RE-READ label=%s tip %s (the lane moved from %s).\n"
            "Your repo cannot fetch. Reset `review` to the tip you were given, "
            "then apply the delta below:\n"
            "  git checkout -q -B review %s\n"
            "  git am <<'PATCH' ... (save the block below to a file and "
            "`git am` it)\n"
            "Review `git diff base..review` again, hardest on what the delta "
            "changed. Report exactly as before, with the first line\n"
            "  CLOUD REVIEW %s %s model=<your exact model id> account=<the same "
            "account>\n"
            "(the tip is %s even though your commit ids differ).\n\n"
            "----- PATCH -----\n%s\n----- END PATCH -----"
            % (label, new_tip[:12], old_tip[:12], old_tip, label, new_tip[:12],
               new_tip[:12], patch))


def reread_branch_message(label, old_tip, new_tip, branch):
    """The follow-up for a re-read on the branch transport: the relay pushed
    the new tip as its own branch, and the session fetches it."""
    names = branch_names(branch)
    return ("RE-READ label=%s tip %s (the lane moved from %s).\n"
            "  git fetch origin %s %s\n"
            "  git checkout -q -B %s origin/%s\n"
            "Review `git diff origin/%s..HEAD` again, hardest on what changed "
            "since %s. Report exactly as before, with the first line\n"
            "  CLOUD REVIEW %s %s model=<your exact model id> account=<the same "
            "account>\n"
            "and push any cure as a NEW branch with `git push origin "
            "HEAD:refs/heads/%s`; the fallback report branch is %s."
            % (label, new_tip[:12], old_tip[:12], names["branch"],
               names["base"], names["branch"], names["branch"], names["base"],
               old_tip[:12], label, new_tip[:12], names["cure"],
               names["report"]))


def nudge_message(label, tip):
    return ("REMINDER label=%s tip %s: no report is on the drop yet. If you are "
            "still working, keep going. If you are done, post the ONE comment "
            "the protocol names, first line `CLOUD REVIEW %s %s model=... "
            "account=...`." % (label, tip[:12], label, tip[:12]))


def idle_message(label, tip):
    return ("CHECK-IN label=%s tip %s: this account has spent nothing for a "
            "while and no report is on the drop. If you are done, post the ONE "
            "comment now (`CLOUD REVIEW %s %s model=... account=...`). If the "
            "post failed or is waiting on an approval, run `gh auth status`, "
            "retry once, and if it still fails end your reply with the whole "
            "report and the exact error." % (label, tip[:12], label, tip[:12]))


def correction_message(label, tip, why):
    """`why` is this module's own parse reason, never comment text."""
    return ("CORRECTION label=%s tip %s: your report did not parse (%s). Post "
            "one new comment in exactly the protocol's shape: first line "
            "`CLOUD REVIEW %s %s model=... account=...`, then `VERDICT:`, "
            "`FINDING-COUNT:` and the numbered findings." % (
                label, tip[:12], why, label, tip[:12]))


# ---------------------------------------------------------------------------
# the drop: read it, and parse one comment STRICTLY
# ---------------------------------------------------------------------------

def read_drop(spec, timeout=90):
    """([{id, body, created_at, url, author}], None) or (None, why)."""
    drop = parse_drop(spec)
    if not drop:
        return None, "drop %r is not owner/repo#N" % spec
    rc, out, err = RUN([gh_bin(), "api", "--paginate",
                        "repos/%s/%s/issues/%d/comments?per_page=100" % drop],
                       timeout=timeout)
    if rc != 0:
        return None, "gh api failed (rc %s): %s" % (rc, (err or out).strip()[:200])
    comments = []
    for page in _json_values(out):
        for c in page if isinstance(page, list) else ():
            if not isinstance(c, dict) or not isinstance(c.get("body"), str):
                continue
            comments.append({
                "id": c.get("id"), "body": c["body"],
                "created_at": c.get("created_at"), "url": c.get("html_url"),
                "author": (c.get("user") or {}).get("login")
                if isinstance(c.get("user"), dict) else None})
    return comments, None


HEADER = re.compile(
    r"CLOUD REVIEW (?P<label>[A-Za-z0-9][A-Za-z0-9._-]{0,79}) "
    r"(?P<tip>[0-9a-f]{12}) model=(?P<model>[A-Za-z0-9][A-Za-z0-9._:\[\]/-]{0,127})"
    r" account=(?P<account>[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190})\Z")
_VERDICT = re.compile(r"VERDICT: (APPROVE|FIX)\Z")
_COUNT = re.compile(r"FINDING-COUNT: ([0-9]{1,3})\Z")
_FINDING = re.compile(r"([0-9]{1,3})[.)] +\[(BLOCKING|MINOR)\] +\[(MEASURED|INFERRED)\]"
                      r" +(\S.*)\Z")
_NUMBERED = re.compile(r"[0-9]{1,3}[.)] ")
_DETAILS_OPEN = re.compile(r"<details>\s*<summary>\s*patch\s*</summary>\s*", re.I)
_DETAILS_CLOSE = re.compile(r"</details>", re.I)
_FENCE = re.compile(r"\A```[a-z]*\n|\n```\s*\Z")


#: The same report under its kind's own header (task/3517): a first review is
#: `REVIEW <label> at <tip12> ...`, a re-read `REREAD ...`; either may be
#: followed by `Branch:` and `Tip:` lines before the verdict.
HEADER_KIND = re.compile(
    r"(?P<kind>REVIEW|REREAD) (?P<label>[A-Za-z0-9][A-Za-z0-9._-]{0,79}) at "
    r"(?P<tip>[0-9a-f]{12}) model=(?P<model>[A-Za-z0-9][A-Za-z0-9._:\[\]/-]"
    r"{0,127}) account=(?P<account>[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]"
    r"{1,190})\Z")
_META = re.compile(r"(Branch|Tip): (\S+)\Z")
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}\Z")


def header_of(body):
    """The header fields of a comment whose first line is a report header, or
    None. The relay attributes a comment to a session by it before a full
    parse. `kind` is CLOUD REVIEW, REVIEW or REREAD."""
    first = str(body or "").lstrip("﻿").split("\n", 1)[0].rstrip("\r ")
    m = HEADER.match(first)
    if m:
        return dict(m.groupdict(), kind="CLOUD REVIEW")
    m = HEADER_KIND.match(first)
    return m.groupdict() if m else None


#: The hand-back header of a BUILD or a CURE round (CURE is round 1, CUREn is
#: round n), followed by exactly `Branch:` and `Tip:` lines.
HANDBACK = re.compile(r"(?P<kind>BUILD|CURE)(?P<n>[2-9]|[1-9][0-9])? "
                      r"(?P<label>[A-Za-z0-9][A-Za-z0-9._-]{0,79})\Z")
_HANDBACK_META = re.compile(r"(Model|Effort): (\S+)\Z")


def handback_label(body):
    """The label of a comment whose first line is a hand-back header, else
    None."""
    first = str(body or "").lstrip("﻿").split("\n", 1)[0].rstrip("\r ")
    m = HANDBACK.match(first)
    return m.group("label") if m else None


def _is_header_line(line):
    return bool(header_of(line) or handback_label(line)
                or line.startswith("CLOUD REVIEW"))


def parse_handback(body):
    """(hand-back, None) or (None, why) for one BUILD or CURE comment. STRICT:
    the first line is the header, the second exactly `Branch: <name>`, the
    third exactly `Tip: <full commit id>`, then optional `Model:` and
    `Effort:` lines, then free text. No second header, Branch or Tip line.
    The reasons never quote the comment."""
    body = str(body or "")
    if len(body.encode("utf-8")) > MAX_REPORT_BYTES:
        return None, "the comment is over %d bytes" % MAX_REPORT_BYTES
    lines = [ln.rstrip() for ln in body.lstrip("﻿").replace(
        "\r\n", "\n").split("\n")]
    m = HANDBACK.match(lines[0])
    if not m:
        return None, "the first line is not a BUILD or CURE header"
    if len(lines) < 3:
        return None, "the Branch and Tip lines are missing"
    branch, tip = _META.match(lines[1]), _META.match(lines[2])
    if not branch or branch.group(1) != "Branch" \
            or not _BRANCH.match(branch.group(2)) or ".." in branch.group(2):
        return None, "the second line is not `Branch: <branch name>`"
    if not tip or tip.group(1) != "Tip" or not _FULL_ID.fullmatch(tip.group(2)):
        return None, "the third line is not `Tip: <full commit id>`"
    extra, rest = {}, lines[3:]
    while rest and _HANDBACK_META.match(rest[0]):
        key, value = _HANDBACK_META.match(rest[0]).groups()
        if key.casefold() in extra:
            return None, "a second %s line" % key
        extra[key.casefold()] = value
        rest = rest[1:]
    if extra.get("effort") is not None and extra["effort"] not in EFFORTS:
        return None, "the Effort line names no known effort"
    for ln in rest:
        left = ln.lstrip()
        if _is_header_line(left) or _META.match(left):
            return None, "a second header, Branch or Tip line"
    n = m.group("n")
    return {"kind": m.group("kind"),
            "round": 0 if m.group("kind") == "BUILD" else int(n or 1),
            "label": m.group("label"), "branch": branch.group(2),
            "tip": tip.group(2), "model": extra.get("model"),
            "effort": extra.get("effort")}, None


def parse_report(body):
    """(report, None) or (None, why) for one drop comment. STRICT:

      * the first line is exactly the header;
      * then, past blank lines, exactly one `VERDICT: APPROVE|FIX` and one
        `FINDING-COUNT: n`, in that order;
      * then the numbered findings, numbered 1..n in order, each opening
        `[BLOCKING|MINOR] [MEASURED|INFERRED]`; a free-text note between the
        count and finding 1 is skipped, and continuation lines are data;
      * at most one `<details><summary>patch</summary>` block, holding a
        `git format-patch` mbox;
      * no second header, verdict or count line anywhere (that is how an
        injected instruction would try to overwrite the first);
      * APPROVE with a BLOCKING finding, or FIX with none, contradicts itself.

    The reasons name the rule broken and never quote the comment."""
    body = str(body or "")
    if len(body.encode("utf-8")) > MAX_REPORT_BYTES:
        return None, "the comment is over %d bytes" % MAX_REPORT_BYTES
    body = body.lstrip("﻿").replace("\r\n", "\n")
    patch = None
    opens = list(_DETAILS_OPEN.finditer(body))
    if len(opens) > 1:
        return None, "more than one patch block"
    if opens:
        start = opens[0]
        close = _DETAILS_CLOSE.search(body, start.end())
        if not close:
            return None, "the patch block is not closed"
        patch = _FENCE.sub("", body[start.end():close.start()].strip("\n")
                           ).strip("\n") + "\n"
        if not patch.startswith("From "):
            return None, "the patch block does not hold a git format-patch mbox"
        rest = body[close.end():]
        if rest.strip():
            return None, "text follows the patch block"
        body = body[:start.start()]
    lines = body.split("\n")
    head = header_of(lines[0])
    if head is None:
        return None, "the first line is not a CLOUD REVIEW header"
    lines = [ln.rstrip() for ln in lines[1:]]
    for ln in lines:
        if _is_header_line(ln.lstrip()):
            return None, "a second CLOUD REVIEW header"
    verdicts = [i for i, ln in enumerate(lines)
                if ln.lstrip().startswith("VERDICT:")]
    counts = [i for i, ln in enumerate(lines)
              if ln.lstrip().startswith("FINDING-COUNT:")]
    if len(verdicts) != 1 or len(counts) != 1:
        return None, "exactly one VERDICT line and one FINDING-COUNT line"
    v, c = verdicts[0], counts[0]
    meta = {}
    for ln in lines[:v]:
        m = _META.match(ln)
        if not ln.strip():
            continue
        if not m or head["kind"] == "CLOUD REVIEW" or m.group(1) in meta:
            return None, "text before the VERDICT or FINDING-COUNT line"
        meta[m.group(1)] = m.group(2)
    if "Tip" in meta and not (_FULL_ID.fullmatch(meta["Tip"])
                              and meta["Tip"].startswith(head["tip"])):
        return None, "the Tip line does not name the header's tip in full"
    if "Branch" in meta and (not _BRANCH.match(meta["Branch"])
                             or ".." in meta["Branch"]):
        return None, "the Branch line does not name a branch"
    if any(ln.strip() for ln in lines[v + 1:c]):
        return None, "text before the VERDICT or FINDING-COUNT line"
    vm, cm = _VERDICT.match(lines[v]), _COUNT.match(lines[c])
    if not vm or not cm:
        return None, "the VERDICT or FINDING-COUNT line is malformed"
    count = int(cm.group(1))
    findings, current = [], None
    for ln in lines[c + 1:]:
        if _NUMBERED.match(ln):
            fm = _FINDING.match(ln)
            if not fm:
                return None, ("finding %d does not open with [BLOCKING|MINOR] "
                              "[MEASURED|INFERRED]" % (len(findings) + 1))
            if int(fm.group(1)) != len(findings) + 1:
                return None, "findings are not numbered 1..n in order"
            current = {"n": len(findings) + 1, "severity": fm.group(2),
                       "basis": fm.group(3), "text": fm.group(4)}
            findings.append(current)
        elif ln.strip():
            if current is None:
                left = ln.lstrip()
                if count == 0:
                    return None, "text follows a zero FINDING-COUNT"
                if _NUMBERED.match(left) or left.startswith((
                        "CLOUD REVIEW", "VERDICT:", "FINDING-COUNT:")):
                    return None, "structured text before the first finding"
                continue        # a free-text note before finding 1
            current["text"] += "\n" + ln
    if count != len(findings):
        return None, "FINDING-COUNT says %d and %d finding(s) are numbered" % (
            count, len(findings))
    verdict = vm.group(1)
    blocking = sum(1 for f in findings if f["severity"] == "BLOCKING")
    if verdict == "APPROVE" and blocking:
        return None, "an APPROVE that carries a BLOCKING finding"
    if verdict == "FIX" and not findings:
        return None, "a FIX that names no finding"
    return dict(head, verdict=verdict, count=count, blocking=blocking,
                findings=findings, patch=patch), None


def finding_paths(report):
    """The `file:line` paths the BLOCKING findings name, project-relative and
    without traversal, in order. The report's own claim; the relay keeps only
    those the lane actually touches."""
    out = []
    for f in report.get("findings") or ():
        if f.get("severity") != "BLOCKING":
            continue
        parts = [p.strip() for p in f["text"].split("\n", 1)[0].split(" / ")]
        where = parts[1] if len(parts) > 1 else ""
        path = where.split(":", 1)[0].strip().strip("`")
        if path and not os.path.isabs(path) and ".." not in path.split("/") \
                and re.fullmatch(r"[A-Za-z0-9._/-]{1,256}", path) \
                and path not in out:
            out.append(path)
    return out


# ---------------------------------------------------------------------------
# the cure: a patch from the drop, re-authored, onto the exact tip
# ---------------------------------------------------------------------------

_PATCH_START = re.compile(r"From (?:[0-9a-f]{40}|[0-9a-f]{64}) ")
_AUTHOR = re.compile(r"\s*([^<>\n]{1,100}?)\s*<([^<>\s]{3,254})>\s*\Z")


def parse_author(spec):
    """(name, email) from `Name <email>`, else None."""
    m = _AUTHOR.match(str(spec or ""))
    return (m.group(1), m.group(2)) if m else None


#: a model id as a token: a family name, a dash, and a version with a digit
#: (claude-opus-5-5, gpt-6-sol); gpt-oss and the like are not models' names
_MODEL_ID = re.compile(r"(?<![a-z0-9])(?:claude|gpt|gemini|grok|kimi|qwen|"
                       r"deepseek|mistral|llama)-(?=[a-z0-9.-]*\d)[a-z0-9.-]+",
                       re.IGNORECASE)
#: a link to an AI tool, with or without its scheme
_AI_LINK = re.compile(r"(?<![a-z0-9.-])(?:https?://)?(?:[a-z0-9-]+\.)*"
                      r"(?:claude\.ai|claude\.com|chatgpt\.com|openai\.com|"
                      r"anthropic\.com|gemini\.google\.com)(?:/|\b)",
                      re.IGNORECASE)


def ai_lines(message):
    """[(n, line)] — every AI authoring line in a commit message.

    STRICTER THAN THE COMMIT RUNG, and on purpose: the rung
    (helm/trailer_rung.py) spares an indented line and a human Co-Authored-By,
    because it guards a human's commit. A cure from a remote session has no
    human co-author and no reason to indent a trailer, so every line is judged
    dedented and EVERY Co-Authored-By counts. It also refuses what
    BUILD_PROTOCOL forbids beyond the trailer: a `Model:` line, a model id
    anywhere in the prose, and a link to an AI tool (a bare session URL too)."""
    out = []
    for n, raw in enumerate(str(message or "").splitlines(), 1):
        line = raw.strip()
        if trailer_rung.offending(line) \
                or line.casefold().startswith(("co-authored-by:", "model:")) \
                or _MODEL_ID.search(line) or _AI_LINK.search(line):
            out.append((n, line))
    return out


def sanitize_mbox(mbox, author):
    """(mbox, stripped, why). Each patch's From: header becomes `author`, and
    every AI authoring line leaves its commit message (the lines between the
    headers and the `---` that opens the diffstat). The diff is not touched."""
    lines = str(mbox or "").replace("\r\n", "\n").split("\n")
    starts = [i for i, ln in enumerate(lines) if _PATCH_START.match(ln)]
    if not starts or starts[0] != 0:
        return None, 0, "not a git format-patch mbox"
    out, stripped = [], 0
    for k, start in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else len(lines)
        patch = lines[start:end]
        try:
            blank = patch.index("")
            sep = patch.index("---", blank)
        except ValueError:
            return None, 0, "patch %d has no message/diff separator" % (k + 1)
        headers, i = [], 0
        while i < blank:
            ln = patch[i]
            if ln.lower().startswith("from:"):
                headers.append("From: %s <%s>" % author)
                i += 1
                while i < blank and patch[i][:1] in (" ", "\t"):
                    i += 1           # a folded From: continues on these lines
                continue
            headers.append(ln)
            i += 1
        message = patch[blank:sep]
        bad = {n for n, _ln in ai_lines("\n".join(message))}
        stripped += len(bad)
        kept = [ln for n, ln in enumerate(message, 1) if n not in bad]
        while len(kept) > 1 and not kept[-1].strip():
            kept.pop()
        out.extend(headers + kept + [""] + patch[sep:])
    return "\n".join(out), stripped, None


def apply_cure(repo, tip, mbox, author, branch):
    """({patch_tip, ref, stripped}, None) or (None, why).

    A scratch clone at EXACTLY the reviewed tip; the sanitized mbox `git am`-ed
    with the cure author as committer and repository hooks off (it is a
    scratch repository, and the checks below are this door's own); then every
    new commit is CHECKED — author and committer are the cure author, and no
    AI authoring line survived. Only then is the result fetched into the
    project repository, as `refs/heads/<branch>`, or, where the shared
    checkout's ref guard refuses a branch born outside a room, under
    `refs/remote-review/`, which it does not guard. Either way the commit is
    in the repository the verdict's --patch-tip names."""
    text, stripped, why = sanitize_mbox(mbox, author)
    if why:
        return None, why
    cures = os.path.join(work_dir(), "cures")
    os.makedirs(cures, mode=0o700, exist_ok=True)
    scratch = tempfile.mkdtemp(prefix="cure-", dir=cures)
    try:
        name, email = author
        steps = [("init", "-q"),
                 ("fetch", "--no-tags", "-q", repo, "%s:refs/heads/cure" % tip),
                 ("checkout", "-q", "cure")]
        for args in steps:
            rc, _out, err = _git(scratch, *args)
            if rc != 0:
                return None, "scratch step `git %s` failed: %s" % (args[0], err)
        env = {"GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}
        rc, _out, err = _git(scratch, "-c", "core.hooksPath=/dev/null",
                             "-c", "commit.gpgsign=false", "am", "-q",
                             env=env, stdin=text.encode("utf-8"))
        if rc != 0:
            _git(scratch, "am", "--abort")
            return None, "the cure patch does not apply on %s: %s" % (
                tip[:12], (err or "").strip()[:200])
        why = verify_cure(scratch, tip, author)
        if why:
            return None, why
        rc, new, err = _git(scratch, "rev-parse", "HEAD")
        if rc != 0:
            return None, "the cure has no head: %s" % err
        ref, why = _fetch_cure(repo, scratch, branch)
        if why:
            return None, why
        return {"patch_tip": new, "ref": ref, "stripped": stripped}, None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def verify_cure(scratch, tip, author, head="HEAD"):
    """None when every commit tip..HEAD is authored and committed by `author`,
    carries no AI authoring line, and changes only plain files; else why.
    This is the guard; the sanitizer is only what makes a clean patch pass
    it. The file-shape clause is the door's own (kimi's read of this lane):
    a patch adding a SYMLINK passed every earlier check, and a checkout of
    the branch it landed on puts a link to anywhere on the reader's disk;
    a binary change is unreviewable text by definition. Both refuse, and so
    do a submodule, a path under .github/ (CI runs with the repository's
    secrets), a .gitmodules change, and a file over MAX_CURE_FILE_BYTES.
    Every cure reaches this door: a patch pasted in a comment and a cure
    branch pushed by the session alike (`branch_cure`)."""
    rc, out, err = _git(scratch, "log", "--format=%H%x1f%an%x1f%ae%x1f%cn"
                        "%x1f%ce%x1f%B%x1e", "%s..%s" % (tip, head))
    if rc != 0:
        return "the cure commits could not be read: %s" % err
    commits = [c for c in out.split("\x1e") if c.strip()]
    if not commits:
        return "the cure patch added no commit"
    for c in commits:
        sha, an, ae, cn, ce, msg = (c.strip("\n").split("\x1f") + [""] * 6)[:6]
        if (an, ae) != tuple(author) or (cn, ce) != tuple(author):
            return "cure commit %s is not authored and committed by the cure " \
                   "author" % sha[:12]
        if ai_lines(msg):
            return "cure commit %s still carries an AI authoring line" % sha[:12]
    # ONLY PLAIN FILES. A symlink or a binary change is never a cure's shape:
    # the patch is text reviewed as text, and neither can be read as text.
    rc, out, err = _git(scratch, "diff-tree", "-r", "--no-commit-id",
                        "--format=", "-r", "%s..%s" % (tip, head))
    if rc != 0:
        return "the cure's changes could not be read: %s" % err
    for line in out.splitlines():
        parts = line.split("\t")
        meta = parts[0].split() if parts else []
        if len(meta) >= 2 and meta[1] == "120000":
            return "cure commit adds a symlink (%s), which a review of text " \
                   "cannot see" % (parts[-1] if parts else "?")
        if len(meta) >= 2 and meta[1] == "160000":
            return "cure commit adds a submodule (%s), which a review of " \
                   "text cannot see" % (parts[-1] if parts else "?")
        path = parts[-1] if len(parts) > 1 else ""
        problem = cure_path_problem(path)
        if problem:
            return "cure commit %s" % problem
        if len(meta) >= 4 and _FULL_ID.fullmatch(meta[3]) \
                and meta[3].strip("0"):
            rc, size, _err = _git(scratch, "cat-file", "-s", meta[3])
            if rc != 0 or not size.isdigit() or int(size) > MAX_CURE_FILE_BYTES:
                return "cure commit writes %s at %s bytes, over the %d-byte " \
                       "limit for source" % (path, size or "?",
                                             MAX_CURE_FILE_BYTES)
    rc, out, err = _git(scratch, "diff", "--stat", "--numstat", tip, head)
    if rc != 0:
        # Fail closed: an unread numstat is an unchecked binary.
        return "the cure's file kinds could not be read: %s" % err
    for line in out.splitlines():
        if line.startswith("-\t"):
            return "cure commit changes a binary file (%s), which a " \
                   "review of text cannot see" % line.split("\t")[-1]
    return None


#: The largest file a cure may write: anything bigger is data, not source.
MAX_CURE_FILE_BYTES = 1024 * 1024
#: Paths a cure never touches: CI definitions run with the repository's
#: secrets on the next push, and git's own metadata is not the tree.
_CURE_FORBIDDEN = (re.compile(r"(?:^|/)\.github/"), re.compile(r"(?:^|/)\.git/"),
                   re.compile(r"(?:^|/)\.gitmodules\Z"))


def cure_path_problem(path):
    """Why a cure may not write `path`, or None."""
    path = str(path or "")
    if not path or os.path.isabs(path) or ".." in path.split("/"):
        return "names an unusable path (%r)" % path[:80]
    for rule in _CURE_FORBIDDEN:
        if rule.search(path):
            return "touches %s, which a remote cure never changes" % path
    return None


def _fetch_cure(repo, scratch, branch):
    for n in range(1, 10):
        name = branch if n == 1 else "%s-%d" % (branch, n)
        for ref in ("refs/heads/" + name, "refs/remote-review/" + name):
            rc, _out, _err = _git(repo, "rev-parse", "-q", "--verify", ref)
            if rc == 0:
                break                           # taken: next suffix
            rc, _out, err = _git(repo, "fetch", "--no-tags", "-q", scratch,
                                 "HEAD:" + ref)
            if rc == 0:
                return ref, None
            if "REFUSED" not in (err or ""):
                return None, "fetching the cure into %s failed: %s" % (
                    repo, (err or "").strip()[:200])
        else:
            return None, "the ref guard refused both namespaces for the cure"
    return None, "no free name for the cure branch %s" % branch


# ---------------------------------------------------------------------------
# state inference
# ---------------------------------------------------------------------------

def target_of(events):
    """The (label, tip, ts, kind) a session is currently asked to read: its
    launch, or the latest re-read delivered after it."""
    target = None
    for e in events:
        if e.get("event") == "launch" and e.get("sid"):
            target = (e.get("label"), e.get("tip"), e.get("ts"), "launch")
        elif e.get("event") == "deliver" and e.get("kind") == "reread" \
                and e.get("ok"):
            target = (e.get("label"), e.get("tip"), e.get("ts"), "reread")
    return target


def infer_state(events, now=None, nudge_after=None, nudges_max=None,
                flat=None, idle_after=None):
    """(state, why) for ONE session from its journal events, oldest first.

    `flat(since_epoch)` answers how many seconds the session's ACCOUNT has
    spent nothing, counted over readings taken at or after `since_epoch`, or
    None when there are too few readings to say. Flat for `idle_after` since
    the session's current read began is SILENT-IDLE, which earns ONE idle
    check-in; flat for as long again after that check-in is UNKNOWN, which the
    relay escalates. Flat for `idle_after` since the LAST nudge, once
    `nudges_max` nudges have already been delivered and no report is on the
    drop, is UNDELIVERED: completion is UNKNOWN, but the report needs manual
    recovery and the session no longer holds a launch slot. That check asks
    `flat` from the last nudge, so a streak that started earlier does not decide
    it, and it is decided before SILENT-IDLE. Without
    the signal the clock alone decides, as below.

    It never guesses toward progress: a launch that named no session, three
    failed sends with no archive signal, and silence past every nudge with no
    flat reading are UNKNOWN, because each is a session nobody can say
    anything true about."""
    now = time.time() if now is None else now
    nudge_after = nudge_after or nudge_after_s()
    nudges_max = nudges_max or max_nudges()
    idle_after = idle_after or idle_flat_s()
    launches = [e for e in events if e.get("event") == "launch"]
    if not launches:
        return UNKNOWN, "no launch is recorded"
    if not any(e.get("sid") for e in launches):
        return UNKNOWN, "the launch reported no session id"
    archived = [e for e in events if e.get("event") == "archived"]
    if archived:
        return ARCHIVED, "a send was refused: %s" % (
            archived[-1].get("error") or "archived")
    label, tip, since, kind = target_of(events)
    if any(e.get("event") == "report" and e.get("label") == label
           and e.get("tip") == tip for e in events):
        return ANSWERED, "a report for %s at %s is on the drop" % (label, tip[:12])
    after = [e for e in events if str(e.get("ts") or "") >= str(since or "")]
    fails = 0
    for e in after:
        if e.get("event") == "deliver":
            fails = 0 if e.get("ok") else fails + 1
    if fails >= SEND_FAILURES_UNKNOWN:
        return UNKNOWN, "%d sends failed with no archive signal" % fails
    nudges = [e for e in after if e.get("event") == "deliver" and e.get("ok")
              and e.get("kind") in ("nudge", "correction", "idle-nudge")]
    if len(nudges) >= nudges_max and flat is not None:
        last_nudge = pk.parse_ts_epoch(nudges[-1].get("ts")) or 0
        still = flat(last_nudge)
        if still is not None and still >= idle_after:
            return UNDELIVERED, (
                "nudged %d time(s); the account has spent nothing for %dm "
                "since the last and no report is on the drop" % (
                    len(nudges), still // 60))
    checkins = [e for e in after if e.get("event") == "deliver" and e.get("ok")
                and e.get("kind") == "idle-nudge"]
    if flat is not None:
        start = pk.parse_ts_epoch(since) or 0
        if checkins:
            last_in = pk.parse_ts_epoch(checkins[-1].get("ts")) or start
            still = flat(last_in)
            if still is not None and still >= idle_after:
                return UNKNOWN, ("silent-idle after a check-in: the account "
                                 "has spent nothing for %dm since it and no "
                                 "report is on the drop" % (still // 60))
        else:
            idle = flat(start)
            if idle is not None and idle >= idle_after:
                return SILENT_IDLE, ("the account has spent nothing for %dm "
                                     "and no report is on the drop (an "
                                     "account-wide signal)" % (idle // 60))
    last = max([pk.parse_ts_epoch(e.get("ts")) or 0 for e in nudges]
               + [pk.parse_ts_epoch(since) or 0])
    silent = now - last
    if len(nudges) >= nudges_max and silent > nudge_after:
        return UNKNOWN, "silent for %dm after %d nudge(s)" % (
            silent // 60, len(nudges))
    if nudges:
        return NUDGED, "nudged %d time(s); silent %dm" % (len(nudges),
                                                          silent // 60)
    if kind == "reread":
        return AWAITING, "a re-read was delivered %dm ago" % (silent // 60)
    return LAUNCHED, "launched %dm ago" % (silent // 60)


def nudge_due(state, events, now=None, nudge_after=None):
    """Whether a session in LAUNCHED, AWAITING or NUDGED has been silent past
    the window since its target or its last nudge."""
    if state not in (LAUNCHED, AWAITING, NUDGED):
        return False             # SILENT-IDLE has its own one check-in
    now = time.time() if now is None else now
    nudge_after = nudge_after or nudge_after_s()
    _label, _tip, since, _kind = target_of(events)
    stamps = [pk.parse_ts_epoch(since) or 0] + [
        pk.parse_ts_epoch(e.get("ts")) or 0 for e in events
        if e.get("event") == "deliver" and e.get("ok")
        and e.get("kind") in ("nudge", "correction", "idle-nudge")
        and str(e.get("ts") or "") >= str(since or "")]
    return now - max(stamps) > nudge_after


def sessions(journal):
    """{session id: [events]} and {row id: session id} folded from the
    journal. A row belongs to the session its latest launch or re-read names."""
    by_sid, by_row = {}, {}
    for e in journal:
        sid = e.get("sid")
        if e.get("event") == "launch":
            if sid:
                by_sid.setdefault(sid, []).append(e)
            by_row[e.get("row")] = sid or ("unlaunched:" + str(e.get("ts")))
            if not sid:
                by_sid.setdefault(by_row[e.get("row")], []).append(e)
        elif sid in by_sid:
            by_sid[sid].append(e)
            if e.get("event") == "deliver" and e.get("kind") == "reread" \
                    and e.get("ok"):
                by_row[e.get("row")] = sid
    return by_sid, by_row
