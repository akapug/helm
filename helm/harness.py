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
    read(handle, limit=3000) -> str                  bounded tail text
    send(handle, text, enter=True)                   keystrokes into the pane
    stop(handle)                                     close the pane

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
import shutil
import subprocess
import time

# The one-line optional-companion pitch (doctor + seat resume share it).
RECOMMENDATION = ("no metaharness detected — helm is metaharness-agnostic and "
                  "runs fine without one, but pane ops (`helm seat resume`) "
                  "need one; optional companion: orca (recommended), herdr "
                  "also supported")


class HarnessError(RuntimeError):
    """A metaharness CLI call failed (missing binary, rc != 0, bad JSON, or an
    error envelope). Message carries the CLI's own words, truncated."""


class _CLIAdapter:
    """Shared subprocess+JSON plumbing. Subclasses set name/bin and translate
    the uniform interface into their CLI's argv + reply shapes."""
    name = None   # adapter id ("orca"/"herdr")
    bin = None    # the CLI binary name looked up on PATH

    def __init__(self, path=None):
        self.path = path or self.bin

    def _run(self, args, timeout=60):
        cmd = [self.path] + list(args)
        label = "%s %s" % (self.name, " ".join(args[:2]))
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise HarnessError("%s: %s" % (label, e))
        if p.returncode != 0:
            raise HarnessError("%s: rc %d — %s" % (
                label, p.returncode, (p.stderr or p.stdout or "").strip()[:300]))
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
            raise HarnessError("%s: %s" % (label, msg or json.dumps(d)[:200]))
        result = d.get("result")
        return result if isinstance(result, dict) else {}

    def _field(self, obj, dotted):
        """Walk result.a.b; a miss is a loud HarnessError naming the path —
        a metaharness upgrade that moves a field must never fail silent."""
        cur = obj
        for part in dotted.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur is None:
            raise HarnessError("%s: reply missing result.%s" % (self.name, dotted))
        return cur


class OrcaAdapter(_CLIAdapter):
    """orca's public terminal CLI (flags live-verified 2026-07-21):
    create --worktree path:<cwd> --title T --command C --json  -> terminal.handle
    list --json -> terminals[]; read --terminal H --limit N --json -> terminal.tail
    send --terminal H --text T [--enter] --json; close --terminal H --json."""
    name = "orca"
    bin = "orca"

    def spawn(self, command, title=None, cwd=None):
        args = ["terminal", "create"]
        if cwd:
            args += ["--worktree", "path:" + cwd]
        if title:
            args += ["--title", title]
        args += ["--command", command, "--json"]
        return self._field(self._run(args), "terminal.handle")

    def list(self):
        r = self._run(["terminal", "list", "--json"])
        return [{"handle": t.get("handle"), "title": t.get("title") or "",
                 "status": "connected" if t.get("connected") else "disconnected"}
                for t in (r.get("terminals") or [])]

    def read(self, handle, limit=3000):
        r = self._run(["terminal", "read", "--terminal", handle,
                       "--limit", str(limit), "--json"])
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
        r = self._run(["pane", "list"])
        return [{"handle": p.get("pane_id"),
                 "title": p.get("label") or "",
                 "status": p.get("agent_status") or "unknown"}
                for p in (r.get("panes") or [])]

    def read(self, handle, limit=3000):
        r = self._run(["pane", "read", handle, "--source", "recent",
                       "--lines", str(limit)])
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
