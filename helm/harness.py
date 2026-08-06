#!/usr/bin/env python3
"""helm harness — the metaharness ADAPTER seam.

CANON: helm is metaharness-AGNOSTIC. It never assumes orca (or any other
terminal workspace manager); each metaharness gets a lightweight adapter here,
detect() picks whichever is actually installed, and every helm verb that
drives panes (seat resume, future watchdog legs) speaks only the uniform
interface below. When NO metaharness is installed, pane ops degrade to a
printed paste line and doctor recommends orca as an OPTIONAL companion
(herdr equally supported).

The uniform pane-op interface (every adapter):
    spawn(command, title=None, cwd=None) -> handle   a new pane running command
    list()  -> [{"handle", "title", "status"}, ...]  live panes
    read(handle, limit=3000, timeout=60) -> str      bounded tail text
    send(handle, text, enter=True)                   keystrokes into the pane
    submit(handle, text) -> (state, detail)          text, THEN Enter, VERIFIED
    stop(handle)                                     close the pane

`send` is the TRANSPORT verb and its success means one thing only: the
metaharness accepted the bytes. `submit` is the TURN verb — see the law below
it — and every caller that wants a seat to take a turn must use it.

OPTIONAL per adapter (module-level `ensure_home_worktree` is the floor every
caller falls back to, so an adapter without it still works):
    ensure_home_worktree(seat, repo_root, base="main") -> path

Adapters shell out to each metaharness's OWN public CLI (subprocess + json,
list-argv only — never a shell string, so arbitrary titles/commands are safe
without quoting games):

  orca   `orca terminal create/list/read/send/close --json`
         (flags live-verified against the installed CLI 2026-07-21).
  herdr  `herdr agent start` + `herdr pane list/read/run/send-text/close`
         (API discovered from the installed CLI's own help + `herdr api
         schema` protocol 16, 2026-07-21; replies are JSON envelopes
         {"id", "result": {...}} / {"id", "error": {code, message}}).

TOKEN LAW: nothing routed through this seam may carry a secret. Callers pass
PATHS (a seat's launch.sh), never expanded launch lines — a pane title, spawn
command, or error echo must never contain ANTHROPIC_AUTH_TOKEN et al. Error
text is truncated and comes only from the metaharness CLI's own stderr.
"""
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import uuid

# The one-line optional-companion pitch (doctor + seat resume share it).
RECOMMENDATION = ("no metaharness detected — helm is metaharness-agnostic and "
                  "runs fine without one, but pane ops (`helm seat resume`) "
                  "need one; optional companion: orca (recommended), herdr "
                  "also supported")


class HarnessError(RuntimeError):
    """A metaharness CLI failure with an optional structured provider code."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def _error_code(body):
    try:
        doc = json.loads(body)
    except (TypeError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    err = doc.get("error")
    return doc.get("code") or (err.get("code") if isinstance(err, dict) else None)


# Every orca DAEMON RPC is bounded by this. A hung daemon must not hang a helm
# verb: the socket gets one short window, then the caller degrades to helm's own
# behaviour. Callers needing a different bound pass `timeout=`; there is
# deliberately no env override, because nothing has needed one.
RPC_TIMEOUT_S = 5


# ---------------------------------------------------------------------------
# SUBMIT — "the metaharness took the bytes" is NOT "the seat took a turn"
# ---------------------------------------------------------------------------
# THE BUG (owner-reported, plural and recurring: "these keep landing and not
# hitting enter"). A seat's own resume directive was TYPED INTO ITS COMPOSER
# AND NEVER SUBMITTED. The seat then sits with its next instruction on screen
# doing nothing — and reads IDLE-AND-HEALTHY to seat_liveness, to proxywatch
# and to the beacons, because all three ask "is this pane alive?" and the pane
# is perfectly alive. It is not a prompt nobody answered; it is a message
# nobody sent.
#
# `send(..., enter=True)` returned success on ORCA ACCEPTED THE BYTES, which is
# a DIFFERENT CLAIM from THE SEAT GOT A TURN, and that gap is the whole defect.
# Three answers are needed, not two, and the third is the one that was missing:
#
#   DELIVERED      the composer no longer holds our text — it went in
#   NOT_DELIVERED  the composer STILL holds our text — it did not
#   UNKNOWN        the pane could not be read — WE DO NOT KNOW
#
# UNKNOWN MUST NEVER COLLAPSE INTO DELIVERED. Measured on this fleet
# 2026-08-05: of 26 live panes, 12 answered `terminal read` with an empty tail
# and every cursor pinned at 0 while their seats were provably working (one had
# posted to chat minutes earlier). A verifier that reads those as "composer is
# empty, therefore it submitted" would mint a false success on nearly half the
# fleet — the exact failure this class already produces, with a receipt.
DELIVERED = "delivered"
NOT_DELIVERED = "not-delivered"
UNKNOWN = "unknown"

# The gap between typing the text and the BARE ENTER that submits it.
#
# MEASURED at the pty 2026-08-05, recording raw bytes in an orca-created pane:
# `orca terminal send --text X --enter` is ALREADY two writes — the text, then
# 0x0d exactly 500ms later — and there is NO bracketed-paste wrapper at any
# size (re-measured with a 2739-byte 20-line payload: one write, then the lone
# CR, +0.500s). So the popular "the --enter is absorbed into the paste" story
# is REFUTED at the transport: orca is not the layer that eats the Enter.
#
# The split below therefore does not exist to beat a transport race. It exists
# so the Enter is an ACT WITH ITS OWN RECEIPT — a second call helm can retry,
# and a seam it can READ ACROSS. Two CLI invocations also widen the real gap to
# whole seconds, which costs nothing and can only help whatever the consuming
# TUI's own ingest window turns out to be.
SUBMIT_SETTLE_S = 0.4
SUBMIT_VERIFY_READS = 3          # bounded repaint window, never unbounded
SUBMIT_VERIFY_INTERVAL_S = 0.8
SUBMIT_READ_LIMIT = 200          # lines; the composer is at the BOTTOM


def _env_float(name, default):
    try:
        return float(os.environ[name])
    except (KeyError, TypeError, ValueError):
        return default


# A collapsed paste chip ("[Pasted text #1 +23 lines]") is UNSENT INPUT that
# does not render as the text we typed. Reading it as "not our text, therefore
# it submitted" is a false DELIVERED on exactly the big multi-line directives
# this bug bites hardest, so it counts as HELD in its own right.
_PASTE_CHIP = re.compile(r"^\[Pasted text\b")


def composer_first_line(text):
    """The first non-empty line of an outgoing payload, whitespace-normalized.

    What a composer RENDERS is this line — Claude wraps rather than truncates,
    so the visible row is this string or a prefix of it. Comparing against the
    whole payload could never match; comparing against nothing at all is how a
    verifier ends up asserting the absence of a complaint.
    """
    for line in (text or "").splitlines():
        if line.strip():
            return " ".join(line.split())
    return ""


def _prompt_line(tail):
    """The live composer line in a pane tail, or None when unresolvable.

    A one-line indirection so this module does not carry a lazy import at three
    call sites; the rule itself lives in seat_lifecycle and stays there.
    """
    from .seat_lifecycle import _current_prompt_line
    return _current_prompt_line(tail)


def composer_body(line):
    """A composer PROMPT LINE -> just the text sitting in it ("" when empty).

    ONE owner for this normalization. It lived in two places for an afternoon —
    here and in the fleet probe — which is how a rule about an invisible
    character starts meaning two different things in two files.

    The glyph and the body are separated by U+00A0, not a space (measured live
    2026-08-05: `❯\\xa0Resuming after compaction…`). The bare `.split()` is
    load-bearing and NOT interchangeable with `.split(" ")`: Python's default
    whitespace class INCLUDES U+00A0, so an empty composer (`❯` alone) and a
    held one both normalize correctly, while splitting on a literal space would
    leave the NBSP attached and make every empty composer look like held text.
    An explicit `.replace("\\xa0", " ")` stood here first; a mutant proved it
    dead code, and a comment is the honest replacement for it.
    """
    return " ".join((line or "").strip().lstrip("❯").split())


def composer_holds(tail, text):
    """Does this pane's LIVE composer still hold `text`? True/False/None.

    None is UNREADABLE — no composer could be located in the tail — and it is
    the value that becomes UNKNOWN. It is returned for a blind pane (empty
    tail), for a pane mid-turn whose composer is not the current frame, and for
    any TUI whose prompt this does not recognise.

    The comparison is against OUR OWN TEXT, not against emptiness, and that is
    what makes it discriminate. A composer can legitimately be non-empty
    without holding our directive — a placeholder ("Press up to edit queued
    messages", measured live), a transient toast, or a human's half-typed
    draft. Only a prefix match against the payload we sent proves OUR message
    is the one still sitting there.
    """
    line = _prompt_line(tail)
    if line is None:
        return None                       # UNREADABLE — never "it went in"
    body = composer_body(line)
    if not body:
        return False                      # empty composer — it went in
    if _PASTE_CHIP.match(body):
        return True                       # a collapsed paste chip IS held text
    want = composer_first_line(text)
    if not want:
        return False                      # a bare Enter can never be "held"
    n = min(len(body), len(want))
    return bool(n) and body[:n] == want[:n]


def _pane_row(t):
    """One metaharness pane -> helm's uniform inventory row.

    Shared by the CLI leg (`orca terminal list --json`) and the RPC leg
    (`terminal.list`) because both emit the SAME terminal object; two copies of
    this mapping is how a field rename starts failing on one path only.

    `preview` (the pane's visible tail) rides along free, but note what it is
    NOT: it is mutable scrollback, so it can only ever be a WEAK hint about who
    a pane belongs to — measured live, only 2 of 25 panes still had their launch
    line in view. Pane->seat identity comes from the process, not from here.
    """
    return {"handle": t.get("handle"), "title": t.get("title") or "",
            "preview": t.get("preview") or "",
            "status": "connected" if t.get("connected") else "disconnected",
            "writable": t.get("writable") is True,
            "pty_id": t.get("ptyId"), "tab_id": t.get("tabId"),
            "leaf_id": t.get("leafId"),
            "worktree_id": t.get("worktreeId"),
            "worktree": t.get("worktreePath"),
            # `orphaned` is the field that was DROPPED here and broke every pane
            # read after the orca .46 remint: a pane can be connected=True and
            # writable=True while its PTY has no live renderer (orphaned=True),
            # and reads against it return empty. It is orca's OWN statement of
            # "this pane has no live backend", and it is the only such statement
            # — there is no helm-side heuristic that distinguishes an orphaned
            # PTY from an idle one. Measured live 2026-07-26: 34 panes, 22
            # orphaned-while-connected, every one reading 0 chars behind a live
            # process. Carry it so _pane_live can stop answering True on it.
            "orphaned": t.get("orphaned") is True,
            # THE SECOND DROPPED FIELD, and the discriminator for a pane helm
            # cannot READ (as opposed to one it cannot reach). Measured live
            # 2026-08-05: 26 panes, and `lastOutputAt is None` selected EXACTLY
            # the 12 whose `terminal read` came back with an empty tail and
            # every cursor at 0 — while connected=True, writable=True and
            # orphaned=False said "fine" on all twelve, and at least one was a
            # seat that had posted to chat minutes earlier. It is orca's own
            # statement that it has never observed output from this pane, so a
            # probe that folds those into "clean" reports an all-clear it never
            # measured. Carried so a composer probe can answer CANNOT-TELL.
            "last_output_at": t.get("lastOutputAt")}


def worktree_panes(path, adapter=None):
    """Panes the metaharness has BOUND to `path` -> ([handles], error).

    THE THING /proc CANNOT SEE (2026-07-30, owner-reported, then measured).
    Worktree GC decided a room was empty by scanning /proc for a process whose
    CWD was inside it. But a pane is a METAHARNESS object that OUTLIVES its
    shell: the shell can be hung up, or its cwd moved, while orca still holds a
    terminal bound to that worktree. GC would hang up the shell, delete the
    directory, and leave the operator's sidebar holding a pane whose working
    directory no longer exists — which renders as a bare command prompt and is
    indistinguishable, from the outside, from someone having killed the agent.

    Measured on this host the morning it was found: one live bash sitting at a
    `(deleted)` cwd, and FIVE panes listed by orca with no worktree path at all.
    The owner reported it repeatedly as "someone killed my seats back to a CWD"
    and was told, more than once, that the seats were fine. He was right and the
    instruments were looking in the wrong place.

    RETURNS AN ERROR RATHER THAN AN EMPTY LIST when the metaharness cannot be
    asked, and callers MUST treat that as a refusal. "I could not look" and
    "nothing is there" are the same value only to code that has stopped caring
    which one it got; here they differ by a live agent. There is no metaharness
    at all -> ([], None), which IS an affirmative empty: nothing can be bound to
    a room by a host that does not exist.

    An adapter that does not carry a pane's worktree binding in its rows is
    UNANSWERABLE too, by the same rule — see `reports_pane_worktree`. Reading
    its rows would produce a bare [] that means "this adapter never says", and
    a bare [] here authorizes a delete.
    """
    ad = adapter if adapter is not None else detect()
    if ad is None:
        return [], None
    if not getattr(ad, "reports_pane_worktree", False):
        return None, ("the %s adapter does not report which worktree a pane is "
                      "bound to — set HELM_METAHARNESS=none to sweep anyway"
                      % getattr(ad, "name", "metaharness"))
    try:
        panes = ad.list()
    except (HarnessError, OSError) as exc:
        return None, "metaharness pane list unavailable: %s" % exc
    # REALPATH, not abspath: every other worktree comparison in this module
    # (`adopt_worktree`, `_native_home_worktree`, `_knows_worktree`) resolves
    # symlinks, because the metaharness's path and helm's `<repo>-wt/<lane>`
    # can reach the same directory through different links. A miss here is not
    # a cosmetic mismatch — it is a delete under a live pane.
    want = os.path.realpath(path).rstrip(os.sep)
    bound = []
    for p in panes:
        wt = p.get("worktree")
        if not wt:
            continue                      # unbound pane belongs to no room
        wt = os.path.realpath(wt).rstrip(os.sep)
        if wt == want or wt.startswith(want + os.sep):
            # A pane with no handle still BLOCKS; it just cannot be named.
            bound.append(p.get("handle") or "?")
    return bound, None


def disposable_worktree_pid(pid, proc_root="/proc"):
    """Whether a metaharness owns this PID as a disposable room placeholder.

    Today Orca is the only adapter with such a process: an inert shell-ready
    bash. Keeping the host fingerprint at the adapter seam prevents worktree GC
    from accumulating Orca/Herdr-specific argv and process-state cases. Any
    unreadable field is NOT disposable; future adapters extend this owner.
    """
    pid = str(pid)
    if not pid.isdigit():
        return False
    base = os.path.join(proc_root, pid)
    try:
        with open(os.path.join(base, "cmdline"), "rb") as f:
            argv = [a for a in f.read(65536).split(b"\0") if a]
        with open(os.path.join(base, "stat"), "rb") as f:
            raw = f.read(65536)
        with open(os.path.join(base, "task", pid, "children"), "rb") as f:
            children = f.read(65536).strip()
    except OSError:
        return False
    if not argv or os.path.basename(os.fsdecode(argv[0])) != "bash" or children:
        return False
    rcfile = None
    for i, arg in enumerate(argv):
        if arg == b"--rcfile" and i + 1 < len(argv):
            rcfile = argv[i + 1]
            break
        if arg.startswith(b"--rcfile="):
            rcfile = arg.split(b"=", 1)[1]
            break
    shell_ready = (b".config/orca/shell-ready",
                   b".config/orca/shell-ready/bash/rcfile")
    if rcfile is None or not any(
            rcfile == p or rcfile.endswith(b"/" + p) for p in shell_ready):
        return False
    close = raw.rfind(b") ")
    fields = raw[close + 2:].split() if close >= 0 else []
    if len(fields) < 6:
        return False
    try:
        state, pgrp, session, tpgid = (
            fields[0], int(fields[2]), int(fields[3]), int(fields[5]))
        n = int(pid)
    except (TypeError, ValueError):
        return False
    return state == b"S" and session == n and pgrp == n and tpgid == pgrp


class _CLIAdapter:
    """Shared subprocess+JSON plumbing. Subclasses set name/bin and translate
    the uniform interface into their CLI's argv + reply shapes."""
    name = None   # adapter id ("orca"/"herdr")
    bin = None    # the CLI binary name looked up on PATH
    # Does this adapter's `list()` row carry `worktree` — the directory the
    # pane is bound to? DEFAULT FALSE, and the default is the whole point:
    # `worktree_panes` is the guard that keeps GC from deleting a room out from
    # under a live pane, and an adapter that never fills the field would hand
    # it an empty list that reads as "nothing is bound here". Unproven means
    # UNANSWERABLE, which refuses; flip this to True in the same commit that
    # makes the adapter's row builder carry the field.
    reports_pane_worktree = False

    def __init__(self, path=None):
        self.path = path or self.bin

    def _run(self, args, timeout=60, env=None):
        """`env` OVERLAYS the ambient environment for this one call (never
        replaces it — the CLI needs HOME/PATH/its own socket vars). It is how
        the shared-tree ref-guard is handed the single bit distinguishing a
        sanctioned worktree creator from a forbidden one, scoped to the call
        rather than leaked into the process (the work/_claims.py pattern)."""
        cmd = [self.path] + list(args)
        label = "%s %s" % (self.name, " ".join(args[:2]))
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout,
                               env=dict(os.environ, **env) if env else None)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise HarnessError("%s: %s" % (label, e))
        if p.returncode != 0:
            body = (p.stderr or p.stdout or "").strip()
            raise HarnessError("%s: rc %d — %s" % (
                label, p.returncode, body[:300]), code=_error_code(body))
        try:
            d = json.loads(p.stdout)
        except ValueError:
            raise HarnessError("%s: unparseable JSON reply (%r)"
                               % (label, (p.stdout or "").strip()[:120]))
        if not isinstance(d, dict):
            raise HarnessError("%s: non-object JSON reply" % label)
        err = d.get("error")
        if err or d.get("ok") is False:   # herdr error envelope / orca ok:false
            msg = (err or {}).get("message") if isinstance(err, dict) else None
            code = (err or {}).get("code") if isinstance(err, dict) else d.get("code")
            raise HarnessError("%s: %s" % (label, msg or json.dumps(d)[:200]),
                               code=code)
        result = d.get("result")
        return result if isinstance(result, dict) else {}

    # -- the TURN verb, shared by every adapter -------------------------------

    def submit(self, handle, text, settle=None, reads=None, interval=None):
        """Give a pane a TURN, and prove it -> (state, detail).

        state is one of DELIVERED / NOT_DELIVERED / UNKNOWN and the third is
        never folded into the first. Defined ONCE here rather than per adapter,
        so orca, herdr and every adapter added later inherit the same contract
        and every caller inherits it through them.

        THREE ACTS, and the split is the point:
          1. TYPE the text with NO Enter.
          2. SETTLE.
          3. A BARE ENTER, as its own call with its own receipt.
          4. READ THE PANE BACK and require that it ADVANCED.

        Step 4 is what `send` never did. `send` succeeded when the metaharness
        accepted the bytes, which is true of every one of the panes the owner
        found sitting with their next instruction typed and unsent.

        FAILURE DIRECTIONS ARE DELIBERATE. A typing failure is NOT_DELIVERED —
        nothing was typed, nothing can be sitting there. An Enter failure falls
        THROUGH to verification rather than guessing, because the text is in
        the composer either way and only the pane can say what happened to it.
        An unreadable pane is UNKNOWN even when the send legs both returned ok.

        THE PRE-READ EXISTS TO CLOSE A FALSE DELIVERED, and it was found on a
        live pane rather than reasoned about. A composer can ALREADY hold text
        when helm types — a human's half-finished draft, or another actuator's.
        Claude appends at the cursor, so the composer's first line is then the
        PRIOR text and no longer ours; "the composer does not start with our
        text" would answer DELIVERED over a pane where nothing was submitted at
        all. When the composer was dirty going in, only an EMPTY composer
        afterwards proves delivery, and anything else is UNKNOWN.
        """
        # An EXPLICIT argument outranks the env override, which outranks the
        # default. The other order silently ignores what the caller asked for.
        if settle is None:
            settle = _env_float("HELM_SUBMIT_SETTLE_S", SUBMIT_SETTLE_S)
        reads = SUBMIT_VERIFY_READS if reads is None else reads
        interval = (SUBMIT_VERIFY_INTERVAL_S if interval is None else interval)
        dirty = self.composer_was_dirty(handle)
        try:
            self.send(handle, text, enter=False)
        except HarnessError as e:
            return NOT_DELIVERED, "pane %s refused the text: %s" % (handle, e)
        if settle > 0:
            time.sleep(settle)
        enter_err = None
        try:
            self.send(handle, "", enter=True)
        except HarnessError as e:
            # The text IS in the composer now. Whether the Enter landed is a
            # question only the pane can answer, so record the error and let
            # the read-back decide instead of asserting either outcome.
            enter_err = str(e)
        state, detail = self.verify_submitted(handle, text, reads=reads,
                                              interval=interval, dirty=dirty)
        if enter_err:
            detail = "%s (the Enter call itself errored: %s)" % (detail,
                                                                 enter_err)
        return state, detail

    def composer_was_dirty(self, handle):
        """Did this pane's composer already hold text? True/False/None.

        None is unreadable, and it propagates: a submit that could not see the
        composer BEFORE typing cannot later claim the composer is clean because
        of anything it typed.
        """
        try:
            tail = self.read(handle, limit=SUBMIT_READ_LIMIT)
        except (HarnessError, OSError):
            return None
        line = _prompt_line(tail)
        return None if line is None else bool(composer_body(line))

    def verify_submitted(self, handle, text, reads=None, interval=None,
                         dirty=False):
        """Did `text` leave the composer? -> (state, detail).

        Polls a BOUNDED repaint window. The first read that proves the composer
        is clear of our text wins; a composer that still holds it after every
        read is NOT_DELIVERED; a pane that never yielded a readable composer is
        UNKNOWN. Separate from `submit` because the same proof is what a probe
        and a retry both need.

        `dirty` is what the composer looked like BEFORE we typed. True or
        UNREADABLE (None) both raise the bar to an EMPTY composer, because with
        foreign text in front of ours "does not start with our text" stops
        meaning anything. Default False keeps the plain case cheap.
        """
        reads = SUBMIT_VERIFY_READS if reads is None else reads
        interval = (SUBMIT_VERIFY_INTERVAL_S if interval is None else interval)
        last = "pane %s never yielded a readable composer" % handle
        proven_held = False
        for n in range(1, max(1, reads) + 1):
            if n > 1 and interval > 0:
                time.sleep(interval)
            try:
                tail = self.read(handle, limit=SUBMIT_READ_LIMIT)
            except (HarnessError, OSError) as e:
                last = "pane %s re-read failed: %s" % (handle, e)
                continue
            held = composer_holds(tail, text)
            if held is None:
                last = ("pane %s returned no readable composer (read %d)"
                        % (handle, n))
                continue
            if not held:
                if dirty is False:
                    return DELIVERED, ("pane %s advanced — the composer no "
                                       "longer holds the text (read %d)"
                                       % (handle, n))
                # The composer was ALREADY dirty (or unreadable) before we
                # typed, so our text is not the first line and "does not start
                # with our text" proves nothing. Only EMPTY proves it here.
                line = _prompt_line(tail)
                if line is not None and not composer_body(line):
                    return DELIVERED, ("pane %s advanced to an EMPTY composer "
                                       "(read %d; it held foreign text before "
                                       "the send, so nothing weaker counts)"
                                       % (handle, n))
                last = ("pane %s held foreign text before the send and its "
                        "composer is still not empty — helm cannot tell whose "
                        "text is in there or whether the turn landed (read %d)"
                        % (handle, n))
                continue
            proven_held = True
            last = ("pane %s STILL holds the text in its composer after %d "
                    "read(s) — typed, never submitted" % (handle, n))
        # A composer that was READ and still holds our text is a PROVEN
        # non-delivery; anything else is an admission that we could not tell,
        # and the two must not share a return value.
        return (NOT_DELIVERED if proven_held else UNKNOWN), last

    def _field(self, obj, dotted):
        """Walk result.a.b; a miss is a loud HarnessError naming the path —
        a metaharness upgrade that moves a field must never fail silent."""
        cur = obj
        for part in dotted.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur is None:
            raise HarnessError("%s: reply missing result.%s" % (self.name, dotted))
        return cur


# ---------------------------------------------------------------------------
# per-seat HOME worktree — the isolation seam (LAYER 1 of the coordination
# substrate design: prd/COORDINATION-SUBSTRATE-DESIGN.md)
# ---------------------------------------------------------------------------
# THE BUG this cures: a spawned seat's cwd DEFAULTED to wherever the operator
# stood — the shared main checkout — so nine fleet agents edited, built and
# stashed the SAME tree. Dirty main blocks every land, seats collide, and
# `git stash` on a shared checkout is not swarm-safe (Ember's law). A seat now
# gets a long-lived private checkout keyed by its own name.
#
# ONE method, THREE metaharness cases, IDENTICAL contract:
#     ensure_home_worktree(seat, repo_root, base="main") -> absolute path
# create-or-REUSE (idempotent — a second spawn of the same seat returns the
# same path), and deliberately NEVER `worktree lock`ed: `helm work claim` locks
# a LANE room because there the lock IS the task lease, but a seat HOME is
# long-lived and a lock would make every prune/remove/gc refuse forever.
#
# The seam above the three implementations is identical, which is the whole
# point: helm is metaharness-AGNOSTIC. herdr honors helm's deterministic path
# natively (`--path` + adopt); orca's worktree CLI cannot (create-only,
# `--name`, no adopt), so orca rides the helm-native floor plus a fail-open
# visibility pass.

SEAT_HOME_DIRNAME = "seats"


def seat_worktree_path(repo_root, seat):
    """`<repo>-wt/seats/<seat>` — the deterministic per-seat HOME. The pure
    function IS the registry key (the lane_path law). It reuses the ONE sibling
    container helm already uses for lane rooms (`<repo>-wt/`, already
    automap-folded), one level down under `seats/` so a seat HOME can never
    collide with a lane room that happens to share a seat's name."""
    return os.path.join(repo_root.rstrip(os.sep) + "-wt", SEAT_HOME_DIRNAME,
                        seat)


def seat_branch(seat):
    """The seat's own long-lived branch — `seat/<seat>`, a namespace disjoint
    from `lane/<lane>` so lane gc/merge logic never mistakes a seat HOME for a
    task room."""
    return "seat/" + seat


def find_repo_root(path=None):
    """The MAIN checkout root for `path` (linked worktrees fold to it via
    --git-common-dir), or None outside a checkout. A REUSE of work's resolver,
    never a second derivation. `path=None`/'' answers None rather than reaching
    for os.getcwd() — a deleted cwd must not raise here."""
    if not path:
        return None
    from .work._lanes import find_root
    return find_root(path)


def _check_seat_name(seat):
    """A seat name becomes a path segment AND a branch name; anything that
    could escape `<repo>-wt/seats/` or be read as a git flag is refused
    loudly rather than silently landing a worktree somewhere else."""
    if not seat or not isinstance(seat, str) or seat != seat.strip() \
            or seat.startswith("-") or seat in (".", "..") \
            or os.sep in seat or (os.altsep and os.altsep in seat):
        raise HarnessError("unsafe seat name %r for a home worktree" % (seat,))


def home_drift(repo_root, seat, base="main"):
    """(behind, ahead) commits between a seat's HOME branch and trunk, or None
    when git cannot say. Pure MEASUREMENT — it moves nothing.

    Worktrees share one object store, so this is answerable from the shared
    checkout without entering (or disturbing) the seat's tree at all."""
    from .work._lanes import _git
    branch = seat_branch(seat)
    rc, out, _err = _git(repo_root, "rev-list", "--left-right", "--count",
                         "%s...%s" % (base, branch))
    if rc != 0 or not out:
        return None            # a pruned base, an unborn branch, a broken repo
    parts = out.split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None            # unparseable is UNKNOWN, never zero
    return int(parts[0]), int(parts[1])


def report_home_drift(repo_root, seat, base="main"):
    """Say it out loud when a REUSED seat home is behind trunk, and do NOTHING
    else. Returns the drift for callers that want it.

    THE BUG THIS CLOSES (console-design, 2026-07-24): a seat home is provisioned
    once, at whatever trunk happened to be, and the reuse leg returned it with no
    further word. The first repair told every author to rebase before working.
    Three different seats then obeyed, gated, and reported — while another land
    moved trunk between their check and report. A true author-side check cannot
    stay true; only the integrator's ff-only landing decision is authoritative.

    WHY IT ONLY REPORTS: auto-rebasing is the one thing this must not do. A
    rebase can conflict, and a tree helm does not own is not helm's to resolve —
    the same law that makes `git stash` unsafe on a shared checkout. Surface the
    drift, name the remedy, let the owner run it. Fail-open in both directions:
    if git cannot answer, nothing is claimed and nothing is printed, because a
    measurement we could not take is not a measurement of zero."""
    drift = home_drift(repo_root, seat, base)
    if not drift or not drift[0]:
        return drift
    behind, ahead = drift
    print("helm seat: NOTE %s's home worktree is %d commit%s BEHIND %s%s — "
          "DO NOT rebase speculatively as the author. Work from the recorded "
          "base; the reviewer binds that exact tip, then the integrator chooses "
          "landing order, rebases the chain, and runs the final exact-tree gate "
          "once. An author-side freshness check can become stale before it is "
          "reported."
          % (seat, behind, "" if behind == 1 else "s", base,
             " (and %d ahead)" % ahead if ahead else ""), file=sys.stderr)
    return drift


def _native_home_worktree(repo_root, seat, base="main"):
    """The canonical FLOOR — helm-native `git worktree add`, no metaharness
    involved. Same shape as work/_claims.py's room provisioning, MINUS the
    `worktree lock` (see the no-lock law above) and minus the lease: a seat
    HOME is not a task lease."""
    from .work._gc import _base, _has_branch
    from .work._lanes import _git, worktrees
    _check_seat_name(seat)
    path = seat_worktree_path(repo_root, seat)
    branch = seat_branch(seat)
    registered = {os.path.realpath(w["path"]) for w in worktrees(repo_root)}
    if os.path.realpath(path) in registered:
        if os.path.isdir(path):
            report_home_drift(repo_root, seat, base)
            return path                 # REUSE — the idempotent leg
        # The admin record outlived the directory (a reseed/rm -rf). Prune
        # only drops records whose checkout is already gone, so this cannot
        # touch a live sibling room.
        _git(repo_root, "worktree", "prune")
    elif os.path.isdir(path):
        raise HarnessError(
            "%s already exists but is not a registered worktree of %s — "
            "inspect it by hand rather than have helm adopt unknown bytes"
            % (path, repo_root))
    if _git(repo_root, "rev-parse", "--verify", "-q", base)[0] != 0:
        base = _base(repo_root)    # main, else the shared checkout's HEAD
    args = (["worktree", "add", path, branch] if _has_branch(repo_root, branch)
            else ["worktree", "add", "-b", branch, path, base])
    # HELM_WORK_CLAIM: the shared-tree ref-guard cannot tell `worktree add -b`
    # apart from a forbidden `checkout -b` — both run with cwd AND toplevel in
    # the shared checkout. So the sanctioned creator announces itself, scoped
    # to this ONE call rather than the process env (work/_claims.py:36).
    rc, _out, err = _git(repo_root, *args, env={"HELM_WORK_CLAIM": "1"})
    if rc != 0:
        raise HarnessError("git worktree add failed for seat %s: %s"
                           % (seat, err or "unknown git error"))
    return path


def ensure_home_worktree(seat, repo_root, base="main"):
    """The FLOOR implementation (headless / HELM_METAHARNESS=none / any adapter
    that does not implement its own): create-or-reuse the seat's private
    checkout with helm-native git and return its absolute path. Every adapter's
    method answers the SAME deterministic path for the same inputs."""
    return _native_home_worktree(repo_root, seat, base=base)


class OrcaAdapter(_CLIAdapter):
    """orca's terminal CLI plus its read-only pane-remint runtime method.

    Public pane verbs use the CLI. `resolve_pane` calls the same local,
    authenticated `terminal.resolvePane` RPC Orca's own orchestration CLI uses
    when a long-lived shell's handle has gone stale. The auth token is read from
    Orca's private runtime metadata and sent only over its Unix socket; it never
    enters argv, logs, errors, or Helm state.
    """
    name = "orca"
    bin = "orca"
    # `_pane_row` maps orca's `worktreePath` onto every row this adapter emits,
    # from both the CLI and the RPC leg, so the pane-binding question is
    # answerable here. Measured live 2026-07-30: a real lane room answered with
    # its pane handle, a non-room answered [].
    reports_pane_worktree = True

    @staticmethod
    def _user_data_path():
        explicit = os.environ.get("ORCA_USER_DATA_PATH")
        if explicit:
            return explicit
        if sys.platform == "darwin":
            return os.path.expanduser("~/Library/Application Support/orca")
        if os.name == "nt":
            return os.path.join(os.environ.get("APPDATA") or "", "orca")
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        return os.path.join(base, "orca")

    def _runtime_call(self, method, params, timeout=RPC_TIMEOUT_S):
        # KILL-SWITCH, checked before the socket is even looked up. The RPC legs
        # reach a REAL daemon holding the owner's live panes and project list,
        # and `_user_data_path()` falls back to $HOME/.config/orca — which the
        # test suite does NOT redirect (tests/_tmphome.py moves HELM_HOME only).
        # So any code path that adopts or lists must be switchable off from the
        # environment, or a unit test silently talks to the live workspace.
        if (os.environ.get("HELM_ORCA_RPC") or "").strip().lower() in \
                ("off", "0", "none", "false"):
            raise HarnessError("orca runtime RPC disabled (HELM_ORCA_RPC)")
        path = os.path.join(self._user_data_path(), "orca-runtime.json")
        try:
            with open(path) as f:
                meta = json.load(f)
        except (OSError, ValueError) as e:
            raise HarnessError("orca runtime metadata unavailable: %s" % e)
        transports = meta.get("transports")
        if not isinstance(transports, list):
            transports = [meta.get("transport")]
        transport = next((t for t in transports if isinstance(t, dict) and
                          t.get("kind") == "unix" and t.get("endpoint")), None)
        token = meta.get("authToken")
        if transport is None or not token or not hasattr(socket, "AF_UNIX"):
            raise HarnessError("orca runtime has no usable local Unix transport")
        request_id = str(uuid.uuid4())
        request = {"id": request_id, "authToken": token,
                   "method": method, "params": params}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(timeout)
                conn.connect(transport["endpoint"])
                conn.sendall((json.dumps(request, separators=(",", ":")) +
                              "\n").encode("utf-8"))
                buf = b""
                while len(buf) <= 1024 * 1024:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        if not raw.strip():
                            continue
                        reply = json.loads(raw.decode("utf-8"))
                        if reply.get("_keepalive"):
                            continue
                        if reply.get("id") != request_id:
                            raise HarnessError(
                                "orca runtime rpc %s: response id mismatch"
                                % method)
                        runtime_id = (reply.get("_meta") or {}).get("runtimeId")
                        if runtime_id and meta.get("runtimeId") and \
                                runtime_id != meta["runtimeId"]:
                            raise HarnessError(
                                "orca runtime changed during rpc %s" % method)
                        if reply.get("ok") is not True:
                            err = reply.get("error") or {}
                            msg = err.get("message") if isinstance(err, dict) else None
                            raise HarnessError(
                                "orca runtime rpc %s: %s"
                                % (method, msg or "error reply without a message"))
                        return reply.get("result") or {}
        except HarnessError:
            raise
        # Every error line NAMES THE METHOD — this client outgrew its
        # pane-resolution birth (accounts.list rides it too), and a hardcoded
        # "pane resolution failed" label sent a live accounts.list timeout
        # diagnosis chasing panes (measured 2026-08-04). The failure class
        # stays in %s (e.g. "timed out", ECONNREFUSED).
        except (OSError, ValueError, TypeError) as e:
            raise HarnessError("orca runtime rpc %s failed: %s" % (method, e))
        raise HarnessError("orca runtime rpc %s returned no reply" % method)

    def resolve_pane(self, pane_key):
        terminal = (self._runtime_call(
            "terminal.resolvePane", {"paneKey": pane_key}).get("terminal") or {})
        return {"handle": terminal.get("handle"),
                "pty_id": terminal.get("ptyId"),
                "tab_id": terminal.get("tabId"),
                "leaf_id": terminal.get("leafId")}

    # -- the FAIL-OPEN rung over the same socket -------------------------------
    # Every adopt/see leg reads orca opportunistically: orca absent, daemon
    # down, socket stale, or a garbage reply must degrade to helm's own
    # behaviour and NEVER crash a helm verb. `_runtime_call` raises; this
    # returns (result, error_text) so a caller can render an honest one-liner
    # without a try/except at every site. It is the only rung the adoption
    # seam uses.

    def rpc(self, method, params, timeout=RPC_TIMEOUT_S):
        """(result, None) on success; (None, "one honest line") on ANY failure.

        Deliberately swallows every exception class the socket path can raise
        — a metaharness is an OPTIONAL companion, so its absence is a normal
        state, not an error the caller must handle."""
        try:
            return self._runtime_call(method, params, timeout=timeout), None
        except HarnessError as e:
            return None, str(e)
        except Exception as e:                    # noqa: BLE001 — fail-open law
            return None, "orca rpc %s: %s" % (method, e)

    def panes(self):
        """([row], error) — every pane orca currently holds, over the RPC
        rather than the CLI. Same row shape as `list()` (one normalizer, see
        `_pane_row`), so a caller can take either source. The RPC carries the
        same fields the CLI does plus `lastOutputAt`, and costs no subprocess.

        FAIL-OPEN: an unavailable orca answers ([], reason) — never an
        exception, and never a bare [] that a caller could mistake for
        "orca is running and has no panes"."""
        result, err = self.rpc("terminal.list", {})
        if err:
            return [], err
        return [_pane_row(t) for t in (result.get("terminals") or [])], None

    def adopt_worktree(self, repo_root, path):
        """Make a helm-CREATED checkout visible on orca's board — (ok, detail).

        Orca's own `orca worktree` CLI has no adopt verb and no --path, so this
        is RPC-only (`repo.update`). The authoritative param shape, read from
        the installed app bundle rather than guessed, is
            {"repo": "<selector>", "updates": {...}}
        with the selector one of `id:`/`name:`/`path:` — NOT a flat `repoId`,
        which answers `invalid_argument: Missing repo selector`.

        Two updates do the work together: `externalWorktreeVisibility: "show"`
        turns on external-worktree discovery for the repo, and
        `importedExternalWorktreePaths` names this checkout explicitly. The
        imported list is READ-MODIFY-WRITTEN, never overwritten — a blind write
        would silently un-adopt every worktree adopted before us.

        FAIL-OPEN in every leg: visibility is a nicety, the checkout is the
        contract. The one case that is reported rather than attempted is a repo
        orca holds as a FOLDER context (see below) — silently reclassifying a
        project in the owner's sidebar is not helm's call to make."""
        repos, err = self.rpc("repo.list", {})
        if err:
            return False, err
        want = os.path.realpath(repo_root)
        row = next((r for r in (repos.get("repos") or [])
                    if isinstance(r, dict) and r.get("path")
                    and os.path.realpath(r["path"]) == want), None)
        if row is None:
            return False, "orca does not have %s registered as a project" % repo_root
        if row.get("kind") != "git":
            # MEASURED 2026-07-24, and it is the reason a helm worktree cannot
            # simply be flipped visible: orca holds helm as kind="folder", and a
            # folder context has no externalWorktreeVisibility field at all —
            # orca never enumerates git worktrees for it (git reported 102
            # worktrees for helm; orca's worktree.list knew 1). The schema DOES
            # accept updates.kind="git", but reclassifying a project rearranges
            # the owner's orca sidebar, so helm reports it and stops.
            return False, ("orca holds %s as a %r context, not a git repo — "
                           "external-worktree discovery never runs for it, so "
                           "adoption needs the project reclassified to git "
                           "first (owner-visible sidebar change; helm will not "
                           "do it silently)" % (repo_root, row.get("kind")))
        imported = [p for p in (row.get("importedExternalWorktreePaths") or [])
                    if isinstance(p, str)]
        if not any(os.path.realpath(p) == os.path.realpath(path)
                   for p in imported):
            imported = imported + [path]
        _, err = self.rpc("repo.update", {
            "repo": "id:" + str(row.get("id")),
            "updates": {"externalWorktreeVisibility": "show",
                        "importedExternalWorktreePaths": imported}})
        if err:
            return False, err
        return True, "adopted into orca (visibility show, %d imported path%s)" \
            % (len(imported), "" if len(imported) == 1 else "s")

    def ensure_home_worktree(self, seat, repo_root, base="main"):
        """Orca's `worktree` CLI is create-ONLY and path-BLIND — `create
        --name` (orca picks the directory), no `--path`, no adopt verb
        (re-verified against the installed CLI 2026-07-24). Letting orca create
        the seat HOME would scatter homes into orca-chosen directories and lose
        helm's deterministic `<repo>-wt/seats/<seat>`, so the checkout is
        helm-native and orca visibility is a best-effort, FAIL-OPEN pass on top.

        Adoption now rides the DAEMON RPC (`adopt_worktree` -> `repo.update`),
        which is the only surface that can adopt at all. It replaces an earlier
        `orca worktree set --worktree path:<p>` attempt that could never work:
        `set` can only select a worktree orca ALREADY knows, and an unadopted
        helm checkout is exactly the case where it does not, so it answered
        selector_not_found every time.

        WHY THE EARLIER READING OF THAT FAILURE WAS WRONG (measured 2026-07-24):
        it was taken as "orca does not auto-discover foreign worktrees". The
        real rule is per-repo. Comparing `git worktree list` against orca's
        `worktree.list` across all 22 registered projects: web-app
        (visibility "show") -> orca knew 10 of 10; example-project ("hide") -> 0 of 10;
        api-service ("hide") -> 2 of 20. Orca discovers external worktrees fine, and
        `externalWorktreeVisibility` is what gates it. helm is the harder case
        for a different reason (a folder context — see `adopt_worktree`).

        Either way the CONTRACT is unchanged: the seat gets and runs in its own
        deterministic checkout, and orca board presence is a fail-open nicety
        layered on top. Design: prd/COORDINATION-SUBSTRATE-DESIGN.md LAYER 1."""
        path = _native_home_worktree(repo_root, seat, base=base)
        self.adopt_worktree(repo_root, path)   # (ok, detail) — nicety, ignored
        return path                            # here; `seat adopt` reports it

    def spawn(self, command, title=None, cwd=None):
        # NOTE (measured 2026-07-24, kept as a signpost rather than as code):
        # `orca terminal create` exposes only --worktree/--title/--command/
        # --focus, while the daemon's TerminalCreateParams also accepts an `env`
        # record — the same CLI-is-the-thin-surface asymmetry as worktree adopt.
        # helm does NOT need it: a pane's env identity rides the minted resume
        # script (sessions.mint_resume_script(env=...)), which works identically
        # on all three metaharness cases instead of only on orca.
        # `--worktree path:X` is an Orca-registry selector, not a filesystem
        # cwd. Normal seat homes are adopted before spawn, but a selector can
        # still miss for an explicit cwd, a resumed lane, fail-open adoption, or
        # discovery lag. Keep the visible pane and let the command carry cwd in
        # that one narrow case; every other Orca failure remains loud.
        def create(with_selector):
            args = ["terminal", "create"]
            if cwd and with_selector:
                args += ["--worktree", "path:" + cwd]
            if title:
                args += ["--title", title]
            cmd = command
            if cwd and not with_selector:
                cmd = "cd %s && exec sh -lc %s" % (
                    shlex.quote(cwd), shlex.quote(command))
            args += ["--command", cmd, "--json"]
            return self._field(self._run(args), "terminal.handle")

        if not cwd:
            return create(False)
        try:
            return create(True)
        except HarnessError as exc:
            if exc.code != "selector_not_found":
                raise
            return create(False)

    def list(self):
        r = self._run(["terminal", "list", "--json"])
        return [_pane_row(t) for t in (r.get("terminals") or [])]

    def read(self, handle, limit=3000, timeout=60):
        r = self._run(["terminal", "read", "--terminal", handle,
                       "--limit", str(limit), "--json"], timeout=timeout)
        tail = (r.get("terminal") or {}).get("tail") or []
        return "\n".join(tail)

    def send(self, handle, text, enter=True):
        args = ["terminal", "send", "--terminal", handle, "--text", text]
        if enter:
            args.append("--enter")
        args.append("--json")
        self._run(args)

    def stop(self, handle):
        self._run(["terminal", "close", "--terminal", handle, "--json"])


class HerdrAdapter(_CLIAdapter):
    """herdr's socket-API CLI (JSON by default — no --json flag; shapes from
    the installed CLI's help + `herdr api schema`, protocol 16, 2026-07-21):
    agent start NAME [--cwd P] --no-focus -- argv…  -> result.agent.pane_id
    pane list -> result.panes[] (pane_id/label/agent_status)
    pane read H --source recent --lines N -> result.read.text
    pane run H CMD (text+Enter) / pane send-text H TEXT (literal, no Enter)
    pane close H. Handles are pane ids — pane verbs need them and agent
    verbs accept them ("legacy pane ids")."""
    name = "herdr"
    bin = "herdr"

    def ensure_home_worktree(self, seat, repo_root, base="main"):
        """herdr owns the richest worktree surface of the three, and it is the
        proof helm's seam is not orca-shaped: `worktree create` takes an
        explicit `--path` (so helm's deterministic location WINS) and
        `worktree open --path` ADOPTS an existing checkout.

        Verified against the installed CLI's own help, 2026-07-24:
          worktree list   [--workspace ID | --cwd PATH] [--json]
          worktree create [--cwd PATH] [--branch N] [--base REF] [--path P]
                          [--label T] [--focus|--no-focus] [--json]
          worktree open   [--cwd PATH] (--path P | --branch N) [--label T]
                          [--no-focus] [--json]

        Reuse is checked through herdr's OWN list (which reads the repo's git
        registry), so a home provisioned by any leg is recognised by every
        other. HELM_WORK_CLAIM rides the CLI env for the create; herdr does the
        git work in its DAEMON, whose environment we cannot reach, so a
        ref-guard refusal (or any other create failure) falls back to the
        helm-native floor and then ADOPTS the result via `worktree open`. The
        deterministic path holds on either leg."""
        _check_seat_name(seat)
        path = seat_worktree_path(repo_root, seat)
        label = "helm seat " + seat
        if self._knows_worktree(repo_root, path) and os.path.isdir(path):
            return path                        # REUSE — the idempotent leg
        try:
            self._run(["worktree", "create", "--cwd", repo_root,
                       "--branch", seat_branch(seat), "--base", base,
                       "--path", path, "--label", label, "--no-focus",
                       "--json"], env={"HELM_WORK_CLAIM": "1"})
            return path
        except HarnessError:
            _native_home_worktree(repo_root, seat, base=base)
        try:
            self._run(["worktree", "open", "--cwd", repo_root,
                       "--path", path, "--label", label, "--no-focus",
                       "--json"])
        except HarnessError:
            pass        # visibility is a nicety; the checkout is the contract
        return path

    def _knows_worktree(self, repo_root, path):
        """Does herdr's registry already carry a checkout at `path`? An
        unavailable herdr answers False — the caller then attempts a create,
        whose own failure falls through to the helm-native floor."""
        try:
            rows = self._run(["worktree", "list", "--cwd", repo_root,
                              "--json"]).get("worktrees") or []
        except HarnessError:
            return False
        real = os.path.realpath(path)
        return any(isinstance(w, dict) and w.get("path")
                   and os.path.realpath(w["path"]) == real for w in rows)

    def spawn(self, command, title=None, cwd=None):
        name = title or ("helm-%d" % int(time.time()))
        args = ["agent", "start", name]
        if cwd:
            args += ["--cwd", cwd]
        # agent start wants an argv, not a command string: wrap in sh -lc so
        # the uniform command-string contract holds.
        args += ["--no-focus", "--", "sh", "-lc", command]
        agent = self._field(self._run(args), "agent")
        handle = agent.get("pane_id") or agent.get("terminal_id")
        if not handle:
            raise HarnessError("herdr: agent_started reply carries no pane_id")
        return handle

    def list(self):
        # NO `worktree` KEY, which is why `reports_pane_worktree` stays False
        # for herdr: `pane list` rows are mapped to pane_id/label/agent_status
        # and nothing here says which directory a pane sits in. The flag is a
        # statement about THIS row builder, not a claim that herdr's CLI cannot
        # answer — the daemon was down when it was last probed (2026-07-30), so
        # it is recorded as unproven. Add the field, flip the flag, in one
        # commit; until then `helm work gc` refuses under herdr and says so,
        # which is the safe direction.
        r = self._run(["pane", "list"])
        return [{"handle": p.get("pane_id"),
                 "title": p.get("label") or "",
                 "status": p.get("agent_status") or "unknown"}
                for p in (r.get("panes") or [])]

    def read(self, handle, limit=3000, timeout=60):
        r = self._run(["pane", "read", handle, "--source", "recent",
                       "--lines", str(limit)], timeout=timeout)
        return (r.get("read") or {}).get("text") or ""

    def send(self, handle, text, enter=True):
        # pane run = text + Enter; pane send-text = literal keystrokes.
        self._run(["pane", "run", handle, text] if enter
                  else ["pane", "send-text", handle, text])

    def stop(self, handle):
        self._run(["pane", "close", handle])


ADAPTERS = {"orca": OrcaAdapter, "herdr": HerdrAdapter}


def detect(env=None, which=None):
    """The best available adapter instance, or None when no metaharness is
    installed. Order: HELM_METAHARNESS=orca|herdr|none is the explicit
    override (none = pane ops off even with both installed); inside a herdr
    session (HERDR_ENV set) herdr wins — panes spawn where the operator
    already lives; otherwise orca first (the recommended companion), herdr
    next. env/which are test seams only."""
    env = os.environ if env is None else env
    which = which or shutil.which
    override = (env.get("HELM_METAHARNESS") or "").strip().lower()
    if override in ("none", "off"):
        return None
    if override in ADAPTERS:
        path = which(ADAPTERS[override].bin)
        return ADAPTERS[override](path) if path else None
    order = ("herdr", "orca") if env.get("HERDR_ENV") else ("orca", "herdr")
    for name in order:
        path = which(ADAPTERS[name].bin)
        if path:
            return ADAPTERS[name](path)
    return None
