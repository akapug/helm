"""The box-inventory provider chain: which machines can helm see.

One seam in the providers.py discipline (a documented plain-JSON contract,
multiple implementations, automatic degradation when an external CLI is
absent). Four rungs, one consent ladder — a box is only ever PROBED at the
rung the owner opted into:

  local       this machine. Always present, zero config — the floor.
  external    an optional PATH-discovered cross-box inventory CLI. Absent is
              SILENT (no warning, no row); present-but-broken warns.
  declared    ``HELM_STORAGE_MATRIX_HOSTS`` — SSH aliases the owner named
              explicitly. Naming a host IS the consent to probe it.
  ssh-config  Host names from the SSH client config, listed as CANDIDATES
              only: ``probe_mode: candidate``, never probed until the owner
              opts in via ``HELM_BOXES_SSH_CONSENT`` (``all``, or a
              comma-separated host list). Only the names are read — no
              other value in that file is ever parsed.

Each provider's ``boxes()`` returns ``(rows, warning-or-None)`` where a row is
plain JSON: ``{host, label, ssh_host, probe_mode, reachable, provider, ...}``.
``inventory()`` merges the chain into the ONE shape
``storage_matrix._normalized_inventory`` already validates:
``({"nodes": [...]}, warning-or-None)``. Merge law: the local floor comes
first and a later rung's local row ENRICHES it (the richer identity wins);
any other host is owned by the first rung that names it — later rungs fill
missing keys only, and an unprobed candidate contributes nothing at all to a
host a richer rung already named, so it can never demote a probeable box.

Bare reads never probe. Nothing here creates I/O beyond reading Host names
and running the external inventory CLI when it is installed.
"""

import json
import os
import re
import shutil
import socket
import subprocess

EXTERNAL_TIMEOUT_S = 90
HOST_RE = re.compile(r"\A[A-Za-z0-9._][A-Za-z0-9._-]{0,127}\Z")
CANDIDATE_NOTE = ("listed from the SSH config, not probed — opt in with "
                  "HELM_BOXES_SSH_CONSENT=all or name this host in it")


def declared_hosts():
    """HELM_STORAGE_MATRIX_HOSTS as an ordered, deduplicated host list; None
    when unset. An invalid name raises before anything could reach SSH."""
    raw = os.environ.get("HELM_STORAGE_MATRIX_HOSTS")
    if raw is None:
        return None
    out = []
    for value in raw.split(","):
        host = value.strip()
        if not host:
            continue
        if not HOST_RE.match(host):
            raise ValueError("invalid host in HELM_STORAGE_MATRIX_HOSTS: %s" % host)
        if host not in out:
            out.append(host)
    return out


def ssh_consent():
    """-> (every, names): the explicit opt-in for ssh-config candidates.
    ``all``/``1`` consents every candidate; otherwise a comma-separated host
    list consents exactly those; unset consents none."""
    raw = (os.environ.get("HELM_BOXES_SSH_CONSENT") or "").strip()
    if raw.lower() in ("all", "1"):
        return True, set()
    return False, {h.strip() for h in raw.split(",") if h.strip()}


class LocalProvider:
    """The floor: this machine, always visible, zero config."""

    name = "local"

    def boxes(self):
        return [{"host": socket.gethostname(), "label": "This box",
                 "probe_mode": "local", "reachable": True,
                 "provider": "local"}], None


class ExternalInventoryProvider:
    """An optional cross-box inventory CLI, discovered on PATH. Absent is
    silent by design — the muscle is optional and this box alone is a
    complete inventory. Rows pass through RAW (normalization owns the shape)
    with a ``provider: external`` stamp so consumers can tell the rung."""

    name = "external"

    def __init__(self, binary=None):
        self.binary = binary or os.environ.get("HELM_BOXES_EXTERNAL_CLI") or "fab"

    def boxes(self):
        found = shutil.which(self.binary)
        if not found:
            return [], None
        try:
            proc = subprocess.run([found, "nodes", "--json"], capture_output=True,
                                  text=True, timeout=EXTERNAL_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return [], "storage inventory failed: %s" % exc
        if proc.returncode != 0:
            return [], "storage inventory failed: %s" % (
                (proc.stderr or proc.stdout or "exit %d" % proc.returncode).strip()[:240])
        try:
            value = json.loads(proc.stdout)
        except ValueError as exc:
            return [], "storage inventory is unreadable: %s" % exc
        if not isinstance(value, dict) or not isinstance(value.get("nodes"), list):
            return [], "storage inventory has no nodes list"
        rows = []
        for raw in value["nodes"]:
            if isinstance(raw, dict):
                raw = dict(raw)
                raw.setdefault("provider", "external")
            rows.append(raw)
        return rows, None


class DeclaredProvider:
    """Owner-declared inventory. A declared host is probeable: writing its
    name into the environment is the explicit opt-in, so no second consent
    flag is required at this rung."""

    name = "declared"

    def boxes(self):
        hosts = declared_hosts() or []
        return [{"host": host, "label": host, "ssh_host": host,
                 "probe_mode": "ssh", "reachable": True,
                 "provider": "declared"} for host in hosts], None


class SshConfigProvider:
    """CANDIDATES from the SSH client config — the ladder's visibility rung.
    Reads ONLY ``Host`` names (never HostName, User, keys, or any other
    value); patterns and invalid names are skipped. Without consent a
    candidate is listed unreachable with the opt-in named in its note, so the
    owner can see the machine exists without helm ever touching it."""

    name = "ssh-config"

    def path(self):
        return os.environ.get("HELM_BOXES_SSH_CONFIG") or \
            os.path.expanduser("~/.ssh/config")

    def boxes(self):
        try:
            with open(self.path(), encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
        except OSError:
            return [], None
        every, named = ssh_consent()
        rows, seen = [], set()
        for line in lines:
            words = line.split("#", 1)[0].split()
            if len(words) < 2 or words[0].lower() != "host":
                continue
            for host in words[1:]:
                # HOST_RE rejects every ssh pattern token (*, ?, !negation)
                if not HOST_RE.match(host) or host in seen:
                    continue
                seen.add(host)
                row = {"host": host, "label": host, "ssh_host": host,
                       "provider": "ssh-config"}
                if every or host in named:
                    row.update(probe_mode="ssh", reachable=True)
                else:
                    row.update(probe_mode="candidate", reachable=False,
                               display_only=True, notes=CANDIDATE_NOTE)
                rows.append(row)
        return rows, None


def chain():
    """The default chain, richest rung first after the local floor, so a
    lower rung can never demote what a richer one measured."""
    return [LocalProvider(), ExternalInventoryProvider(), DeclaredProvider(),
            SshConfigProvider()]


def inventory(providers=None):
    """-> ({"nodes": rows}, warning-or-None): the merged chain in the shape
    storage_matrix._normalized_inventory validates. Never empty: the local
    floor guarantees at least this machine. Non-dict rows pass through so the
    validator owns that warning."""
    nodes, warnings, by_host = [], [], {}
    local_row = None

    def register(row):
        for key in (row.get("host"), row.get("ssh_host")):
            if isinstance(key, str) and key:
                by_host.setdefault(key, row)

    for provider in (chain() if providers is None else providers):
        rows, warning = provider.boxes()
        if warning:
            warnings.append(warning)
        for row in rows:
            if not isinstance(row, dict):
                nodes.append(row)
                continue
            if row.get("probe_mode") == "local":
                if local_row is None:
                    local_row = dict(row)
                    nodes.append(local_row)
                else:  # a richer rung's view of THIS machine enriches the floor
                    local_row.update({k: v for k, v in row.items()
                                      if v is not None})
                register(local_row)
                continue
            keys = [k for k in (row.get("host"), row.get("ssh_host"))
                    if isinstance(k, str) and k]
            known = next((by_host[k] for k in keys if k in by_host), None)
            if known is not None:
                # an unprobed candidate contributes NOTHING to a host a richer
                # rung already named: its display_only/notes riding in through
                # setdefault would demote a probeable row (a declared alias is
                # usually ALSO in the ssh config — the common case)
                if row.get("probe_mode") != "candidate":
                    for key, value in row.items():
                        known.setdefault(key, value)
                continue
            row = dict(row)
            nodes.append(row)
            register(row)
    return {"nodes": nodes}, "; ".join(warnings) or None
