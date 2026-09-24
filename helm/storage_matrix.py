"""Bounded cross-box storage probes and their durable owner-facing snapshot.

Measurement is EXPLICIT: the web and the bare CLI read the last atomic snapshot;
only ``helm storage-matrix --measure`` creates I/O or crosses SSH. Every target
uses the same stdlib probe so the matrix compares storage rather than tools.
Which boxes exist is the provider chain's business (helm/boxes.py — local
floor, optional external inventory, declared hosts, unprobed ssh-config
candidates); this module validates the shape and runs the probes.
"""

import datetime as dt
import inspect
import json
import math
import os
import random
import shlex
import socket
import statistics
import subprocess
import sys
import tempfile
import time

from . import boxes, eventledger, home, pk, scratch

SCHEMA = 1
SEQUENTIAL_BYTES = 64 * 1024 * 1024
SEQUENTIAL_BLOCK_BYTES = 1024 * 1024
RANDOM_BLOCK_BYTES = 4096
RANDOM_OPERATIONS = 4096
FSYNC_OPERATIONS = 24
MIN_FREE_BYTES = SEQUENTIAL_BYTES * 3
STALE_S = 7 * 24 * 60 * 60
PROBE_TIMEOUT_S = 120
_HOST_RE = boxes.HOST_RE
_METRIC_KEYS = ("seq_write_mib_s", "seq_read_mib_s", "random_write_iops",
                "random_read_iops", "fsync_p50_ms", "fsync_p95_ms")
_USAGE = "helm storage-matrix [--measure] [--json]"


def path():
    return os.environ.get("HELM_STORAGE_MATRIX") or os.path.join(
        home.global_dir(), ".state", "storage-matrix.json")


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _stamp(value=None):
    value = value or _now()
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _rate_mib(size, elapsed):
    return round(size / (1024 * 1024) / elapsed, 2)


def _percentile(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, math.ceil(len(values) * q) - 1)]


def _evict(fd):
    advise = getattr(os, "posix_fadvise", None)
    flag = getattr(os, "POSIX_FADV_DONTNEED", None)
    if advise is None or flag is None:
        return False
    try:
        advise(fd, 0, 0, flag)
    except OSError:
        return False
    return True


def _pwrite(fd, value, offset):
    write = getattr(os, "pwrite", None)
    if write is not None:
        return write(fd, value, offset)
    os.lseek(fd, offset, os.SEEK_SET)
    return os.write(fd, value)


def _pwrite_all(fd, value, offset):
    written = 0
    while written < len(value):
        size = _pwrite(fd, value[written:], offset + written)
        if not size:
            raise OSError("short random write")
        written += size
    return written


def _pread(fd, size, offset):
    read = getattr(os, "pread", None)
    if read is not None:
        return read(fd, size, offset)
    os.lseek(fd, offset, os.SEEK_SET)
    return os.read(fd, size)


def _probe(target):
    """One bounded same-file probe. The caller owns target identity/error scope."""
    stat = os.statvfs(target)
    free = stat.f_bavail * stat.f_frsize
    if free < MIN_FREE_BYTES:
        raise OSError("less than 192 MiB free at target")
    fd, probe = tempfile.mkstemp(prefix=".helm-storage-matrix-", dir=target)
    payload = (b"helm-storage-matrix\n" *
               (SEQUENTIAL_BLOCK_BYTES // len(b"helm-storage-matrix\n") + 1))[
                   :SEQUENTIAL_BLOCK_BYTES]
    small = payload[:RANDOM_BLOCK_BYTES]
    rng = random.Random(195)
    offsets = [rng.randrange(0, SEQUENTIAL_BYTES // RANDOM_BLOCK_BYTES) *
               RANDOM_BLOCK_BYTES for _ in range(RANDOM_OPERATIONS)]
    try:
        started = time.monotonic()
        for _ in range(SEQUENTIAL_BYTES // SEQUENTIAL_BLOCK_BYTES):
            os.write(fd, payload)
        os.fsync(fd)
        seq_write = _rate_mib(SEQUENTIAL_BYTES, time.monotonic() - started)

        seq_evicted = _evict(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        started = time.monotonic()
        read = 0
        while read < SEQUENTIAL_BYTES:
            block = os.read(fd, SEQUENTIAL_BLOCK_BYTES)
            if not block:
                break
            read += len(block)
        if read != SEQUENTIAL_BYTES:
            raise OSError("short sequential read")
        seq_read = _rate_mib(read, time.monotonic() - started)

        started = time.monotonic()
        for offset in offsets:
            _pwrite_all(fd, small, offset)
        os.fsync(fd)
        random_write = round(RANDOM_OPERATIONS / (time.monotonic() - started), 2)

        random_evicted = _evict(fd)
        started = time.monotonic()
        for offset in offsets:
            if len(_pread(fd, RANDOM_BLOCK_BYTES, offset)) != RANDOM_BLOCK_BYTES:
                raise OSError("short random read")
        random_read = round(RANDOM_OPERATIONS / (time.monotonic() - started), 2)

        latencies = []
        for i in range(FSYNC_OPERATIONS):
            _pwrite_all(fd, small, (i * RANDOM_BLOCK_BYTES) % SEQUENTIAL_BYTES)
            started = time.monotonic_ns()
            os.fsync(fd)
            latencies.append((time.monotonic_ns() - started) / 1_000_000)
        return {
            "seq_write_mib_s": seq_write,
            "seq_read_mib_s": seq_read,
            "random_write_iops": random_write,
            "random_read_iops": random_read,
            "fsync_p50_ms": round(statistics.median(latencies), 3),
            "fsync_p95_ms": round(_percentile(latencies, 0.95), 3),
            "seq_read_cache_evicted": seq_evicted,
            "random_read_cache_evicted": random_evicted,
            "cache_evict_supported": seq_evicted and random_evicted,
            "free_bytes_before": free,
        }
    finally:
        os.close(fd)
        try:
            os.unlink(probe)
        except FileNotFoundError:
            pass


def _method():
    return {
        "engine": "python-stdlib-v1",
        "sequential_bytes": SEQUENTIAL_BYTES,
        "sequential_block_bytes": SEQUENTIAL_BLOCK_BYTES,
        "random_block_bytes": RANDOM_BLOCK_BYTES,
        "random_operations": RANDOM_OPERATIONS,
        "fsync_operations": FSYNC_OPERATIONS,
        "random_seed": 195,
        "notes": ("same-file buffered I/O; final fsync included in write rates; "
                  "best-effort POSIX_FADV_DONTNEED before reads; read bars exclude "
                  "tiers where cache eviction was unavailable; one bounded snapshot, "
                  "not a device specification"),
    }


def _remote_program():
    functions = (_rate_mib, _percentile, _evict, _pwrite, _pwrite_all, _pread,
                 _probe, _method, _stamp, _now)
    constants = (
        "SEQUENTIAL_BYTES = %d\nSEQUENTIAL_BLOCK_BYTES = %d\n"
        "RANDOM_BLOCK_BYTES = %d\nRANDOM_OPERATIONS = %d\n"
        "FSYNC_OPERATIONS = %d\nMIN_FREE_BYTES = %d\n" %
        (SEQUENTIAL_BYTES, SEQUENTIAL_BLOCK_BYTES, RANDOM_BLOCK_BYTES,
         RANDOM_OPERATIONS, FSYNC_OPERATIONS, MIN_FREE_BYTES))
    source = "import datetime as dt\nimport json\nimport math\nimport os\n" \
             "import random\nimport statistics\nimport sys\nimport tempfile\nimport time\n" \
             + constants + "\n".join(inspect.getsource(f) for f in functions)
    return source + "\ntry:\n" \
        "    result = {'status': 'ok', 'measured_at': _stamp(), " \
        "'method': _method(), 'metrics': _probe(sys.argv[1])}\n" \
        "except Exception as exc:\n" \
        "    result = {'status': 'unavailable', 'measured_at': _stamp(), " \
        "'method': _method(), 'error': '%s: %s' % (type(exc).__name__, exc)}\n" \
        "print(json.dumps(result, sort_keys=True))\n"


def _remote_probe(host, target):
    if not _HOST_RE.match(host or ""):
        raise ValueError("invalid SSH host name")
    command = "python3 - %s" % shlex.quote(target)
    proc = subprocess.run(
        ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
         "--", host, command], input=_remote_program(), capture_output=True, text=True,
        timeout=PROBE_TIMEOUT_S)
    line = next((x for x in reversed(proc.stdout.splitlines()) if x.strip()), "")
    try:
        result = json.loads(line)
    except ValueError:
        detail = (proc.stderr or proc.stdout or "exit %d" % proc.returncode).strip()
        raise OSError("remote probe returned no JSON: %s" % detail[:240])
    if not isinstance(result, dict):
        raise OSError("remote probe returned a non-object")
    return result


def _normalized_inventory(value):
    nodes, warnings, seen = [], [], set()
    for index, raw in enumerate(value["nodes"]):
        if not isinstance(raw, dict):
            warnings.append("storage inventory node %d is not an object" % index)
            continue
        host = raw.get("host")
        if not isinstance(host, str) or not _HOST_RE.match(host):
            warnings.append("storage inventory node %d has an invalid host" % index)
            continue
        if host in seen:
            warnings.append("storage inventory has duplicate host %s; first row kept" % host)
            continue
        seen.add(host)
        node, issues = dict(raw), []
        label = node.get("label")
        if not isinstance(label, str) or not label:
            node["label"] = host
            issues.append("label")
        for key in ("reachable", "display_only"):
            if key in node and not isinstance(node[key], bool):
                node[key] = False
                issues.append(key)
        for key in ("probe_mode", "device_kind", "notes", "fab_hot_base"):
            if key in node and node[key] is not None and not isinstance(node[key], str):
                node.pop(key)
                issues.append(key)
        ssh_host = node.get("ssh_host")
        if ssh_host not in (None, "") and (not isinstance(ssh_host, str) or
                                           not _HOST_RE.match(ssh_host)):
            node["ssh_host"] = host
            issues.append("ssh_host")
        order = node.get("inventory_order", index)
        if not isinstance(order, (int, float)) or isinstance(order, bool) or \
                not math.isfinite(order):
            order = index
            issues.append("inventory_order")
        node["inventory_order"] = order
        if issues:
            warning = "storage inventory %s has invalid %s" % (host, ", ".join(issues))
            warnings.append(warning)
            note = node.get("notes") if isinstance(node.get("notes"), str) else ""
            node["notes"] = warning + (("; " + note) if note else "")
            if node.get("probe_mode") != "local":
                node["device_kind"] = (node.get("device_kind")
                                       if isinstance(node.get("device_kind"), str)
                                       else "standby")
                node["reachable"] = False
                node["display_only"] = True
        nodes.append(node)
    out = dict(value)
    out["nodes"] = nodes
    return out, "; ".join(warnings) or None


def _inventory():
    """The box-inventory provider chain (helm/boxes.py), normalized. The chain
    owns discovery — the local floor, the optional external inventory CLI,
    owner-declared hosts, and unprobed ssh-config candidates; this module
    stays the shape validator and the probe engine."""
    value, warning = boxes.inventory()
    normalized, issues = _normalized_inventory(value)
    return normalized, "; ".join(x for x in (warning, issues) if x) or None


def _requested_hosts():
    return boxes.declared_hosts()


def _local_node(inventory):
    nodes = (inventory or {}).get("nodes") or []
    return next((n for n in nodes if n.get("probe_mode") == "local"), None)


def _remote_nodes(inventory, requested):
    nodes = (inventory or {}).get("nodes") or []
    by_host = {n.get("host"): n for n in nodes if n.get("host")}
    if requested is not None:
        return [by_host.get(host) or {"host": host, "reachable": False,
                                      "notes": "host is not in storage inventory"}
                for host in requested]
    return [n for n in nodes if n.get("probe_mode") != "local" and
            (n.get("provider", "external") != "external" or
             n.get("device_kind") in ("fab-node", "standby"))]


def _local_disk_target(local):
    target = (local or {}).get("fab_hot_base")
    if target and os.path.isdir(target) and os.access(target, os.W_OK):
        return target, "host-local scratch storage"
    target, why = scratch.resolve("durable", "storage-matrix")
    if not target:
        raise OSError(why)
    return target, "durable scratch (%s)" % why


def targets():
    inventory, inventory_error = _inventory()
    requested = _requested_hosts()
    local = _local_node(inventory)
    local_host = (local or {}).get("host") or socket.gethostname()
    local_label = (local or {}).get("label") or "This box"
    try:
        disk, disk_label = _local_disk_target(local)
        disk_error = None
    except OSError as exc:
        disk, disk_label = None, "local durable storage"
        disk_error = "%s: %s" % (type(exc).__name__, exc)
    out = [
        {"box": local_host, "box_label": local_label, "tier": "local-disk",
         "tier_label": "Local disk", "transport": "local", "path": disk,
         "path_label": disk_label, "reachable": disk is not None,
         "unavailable_reason": disk_error, "order": -2},
        {"box": local_host, "box_label": local_label, "tier": "memory",
         "tier_label": "Memory", "transport": "local", "path": "/dev/shm",
         "path_label": "/dev/shm", "reachable": os.path.isdir("/dev/shm"),
         "order": -1},
    ]
    for node in _remote_nodes(inventory, requested):
        host = node.get("host") or "unknown"
        base = {"box": host, "box_label": node.get("label") or host,
                "transport": "ssh", "ssh_host": node.get("ssh_host") or host,
                "reachable": bool(node.get("reachable")) and
                not bool(node.get("display_only")),
                "order": node.get("inventory_order", 999),
                "unavailable_reason": node.get("notes") or
                ("inventory reports this box unreachable" if not node.get("reachable")
                 else "inventory marks this box display-only")}
        out.append(dict(base, tier="local-disk", tier_label="Local disk",
                        path=node.get("fab_hot_base"), path_label="host-local scratch storage"))
        out.append(dict(base, tier="memory", tier_label="Memory",
                        path="/dev/shm", path_label="/dev/shm"))
    return out, inventory_error


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and \
        math.isfinite(value) and value >= 0


def _validated_metrics(value):
    if not isinstance(value, dict) or any(
            not _number(value.get(key)) for key in _METRIC_KEYS):
        raise ValueError("metrics are incomplete")
    flags = ("seq_read_cache_evicted", "random_read_cache_evicted",
             "cache_evict_supported")
    for key in flags:
        if not isinstance(value.get(key), bool):
            raise ValueError("cache-eviction evidence is incomplete")
    if value["cache_evict_supported"] != (value["seq_read_cache_evicted"] and
                                           value["random_read_cache_evicted"]):
        raise ValueError("cache-eviction evidence is inconsistent")
    out = {key: value[key] for key in _METRIC_KEYS + flags}
    if "free_bytes_before" in value:
        if not _number(value["free_bytes_before"]):
            raise ValueError("free_bytes_before is invalid")
        out["free_bytes_before"] = value["free_bytes_before"]
    return out


def _snapshot_status(rows, inventory_error=None):
    ok = sum(row.get("status") == "ok" for row in rows)
    return "complete" if rows and ok == len(rows) and not inventory_error else \
           "partial" if ok else "unavailable"


def _row(target):
    row = {k: target.get(k) for k in (
        "box", "box_label", "tier", "tier_label", "transport", "path_label",
        "order")}
    row["id"] = "%s:%s" % (row["box"], row["tier"])
    row["measured_at"] = _stamp()
    if not target.get("reachable"):
        row.update(status="unavailable", error=target.get("unavailable_reason") or
                   "target is unavailable")
        return row
    if not target.get("path"):
        row.update(status="unavailable", error="storage path is not in inventory")
        return row
    try:
        result = ({"status": "ok", "metrics": _probe(target["path"]),
                   "method": _method(), "measured_at": _stamp()}
                  if target["transport"] == "local" else
                  _remote_probe(target["ssh_host"], target["path"]))
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        row.update(status="unavailable", error="%s: %s" %
                   (type(exc).__name__, exc))
        return row
    stamp = result.get("measured_at")
    if stamp is not None and not isinstance(stamp, str):
        row.update(status="unavailable", error="probe returned invalid measured_at")
        return row
    row["measured_at"] = stamp or row["measured_at"]
    if result.get("status") != "ok":
        row.update(status="unavailable", error=str(result.get("error") or
                                                    "probe did not succeed"))
        return row
    try:
        metrics = _validated_metrics(result.get("metrics"))
    except ValueError as exc:
        row.update(status="unavailable", error="probe returned invalid %s" % exc)
        return row
    row.update(status="ok", metrics=metrics)
    return row


def measure(now=None):
    artifact = path()
    with eventledger.locked(artifact) as acquired:
        if not acquired:
            raise OSError("measurement lock is unavailable")
        started = time.monotonic()
        measured_at = _stamp(now)
        found, inventory_error = targets()
        rows = [_row(target) for target in found]
        ok = sum(row["status"] == "ok" for row in rows)
        snapshot = {
            "schema": SCHEMA,
            "measured_at": measured_at,
            "duration_s": round(time.monotonic() - started, 3),
            "status": _snapshot_status(rows, inventory_error),
            "method": _method(),
            "inventory_error": inventory_error,
            "rows": rows,
        }
        _validated(snapshot)
        pk.write_json(artifact, snapshot)
    pk.event("storage-matrix-measure", artifact,
             "%d/%d storage tiers measured" % (ok, len(rows)))
    return snapshot


def _validated(value):
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ValueError("unsupported or missing schema")
    if not isinstance(value.get("measured_at"), str):
        raise ValueError("missing measured_at")
    if not _number(value.get("duration_s")):
        raise ValueError("invalid duration_s")
    method = value.get("method")
    if not isinstance(method, dict) or not isinstance(method.get("engine"), str) or \
            not isinstance(method.get("notes"), str):
        raise ValueError("measurement method is incomplete")
    for key in ("sequential_bytes", "sequential_block_bytes", "random_block_bytes",
                "random_operations", "fsync_operations"):
        if not _number(method.get(key)) or method[key] <= 0:
            raise ValueError("measurement method is incomplete")
    if value.get("inventory_error") is not None and \
            not isinstance(value.get("inventory_error"), str):
        raise ValueError("invalid inventory_error")
    rows = value.get("rows")
    if not isinstance(rows, list):
        raise ValueError("rows is not a list")
    seen, tiers_by_box = set(), {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("row is not an object")
        for key in ("id", "box", "tier", "box_label", "tier_label",
                    "transport", "path_label", "measured_at"):
            if not isinstance(row.get(key), str) or not row[key]:
                raise ValueError("row %s is missing" % key)
        if row["tier"] not in ("local-disk", "memory"):
            raise ValueError("unknown storage tier: %s" % row["tier"])
        if row["id"] != "%s:%s" % (row["box"], row["tier"]):
            raise ValueError("row identity does not match box:tier")
        if row["id"] in seen:
            raise ValueError("duplicate row identity: %s" % row["id"])
        seen.add(row["id"])
        tiers_by_box.setdefault(row["box"], set()).add(row["tier"])
        order = row.get("order")
        if not isinstance(order, (int, float)) or isinstance(order, bool) or \
                not math.isfinite(order):
            raise ValueError("invalid row order")
        if row.get("status") not in ("ok", "unavailable"):
            raise ValueError("invalid row status")
        if row["status"] == "ok":
            row["metrics"] = _validated_metrics(row.get("metrics"))
        elif not isinstance(row.get("error"), str) or not row["error"]:
            raise ValueError("unavailable row has no reason")
    required_tiers = {"local-disk", "memory"}
    if not tiers_by_box:
        raise ValueError("snapshot has no storage boxes")
    incomplete = sorted(box for box, tiers in tiers_by_box.items()
                        if tiers != required_tiers)
    if incomplete:
        raise ValueError("storage tier pair is incomplete for: %s" % ", ".join(incomplete))
    expected = _snapshot_status(rows, value.get("inventory_error"))
    if value.get("status") != expected:
        raise ValueError("snapshot status does not match rows: expected %s" % expected)
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("snapshot is not strict JSON: %s" % exc) from exc
    return value


def _parse_stamp(value):
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            dt.timezone.utc)
    except (AttributeError, ValueError):
        return None


def view(now=None):
    artifact = path()
    if not os.path.exists(artifact):
        return {"state": "missing", "message":
                "not measured yet — run helm storage-matrix --measure"}
    value = pk.read_json(artifact)
    try:
        value = _validated(value)
    except ValueError as exc:
        return {"state": "unavailable", "message":
                "measurement unreadable — %s" % exc}
    measured = _parse_stamp(value["measured_at"])
    current = now or _now()
    if measured is None:
        age = None
        message = "measurement time unknown — values are shown, but their age cannot be trusted"
    else:
        age = (current.astimezone(dt.timezone.utc) - measured).total_seconds()
        if age < -300:
            age = None
            message = "measurement time is in the future — age cannot be trusted"
        else:
            age = max(0, age)
            message = None
    out = dict(value)
    out.update(state="ok", age_s=age, stale=age is not None and age > STALE_S,
               age_message=message, stale_after_s=STALE_S)
    return out


def _age_text(seconds):
    if seconds is None:
        return "unknown age"
    seconds = int(seconds)
    if seconds < 60:
        return "%ds ago" % seconds
    if seconds < 3600:
        return "%dm ago" % (seconds // 60)
    if seconds < 86400:
        return "%dh ago" % (seconds // 3600)
    return "%dd ago" % (seconds // 86400)


def _print_human(value):
    if value.get("state") != "ok":
        print("helm storage-matrix: " + value["message"])
        return 1
    rows = value["rows"]
    ok = sum(row["status"] == "ok" for row in rows)
    prefix = "STALE · " if value["stale"] else ""
    age = value.get("age_message") or _age_text(value.get("age_s"))
    print("storage matrix — %smeasured %s · %d/%d tiers" %
          (prefix, age, ok, len(rows)))
    for row in rows:
        label = "%s / %s" % (row.get("box_label") or row["box"],
                              row.get("tier_label") or row["tier"])
        if row["status"] != "ok":
            print("  %-30s UNAVAILABLE — %s" % (label, row["error"]))
            continue
        m = row["metrics"]
        cached = [name for name, key in (("sequential read", "seq_read_cache_evicted"),
                                          ("random read", "random_read_cache_evicted"))
                  if not m[key]]
        note = " · cache not cleared: " + ", ".join(cached) if cached else ""
        print("  %-30s write %8.1f MiB/s · read %8.1f · random %8.0f/%8.0f IOPS · fsync p95 %7.3f ms%s" %
              (label, m["seq_write_mib_s"], m["seq_read_mib_s"],
               m["random_write_iops"], m["random_read_iops"],
               m["fsync_p95_ms"], note))
    if value.get("inventory_error"):
        print("  inventory: " + value["inventory_error"])
    return 0 if value["status"] == "complete" else 1


def cmd_storage_matrix(args):
    args = list(args or [])
    from .cli import guard_tail
    rc = guard_tail("helm storage-matrix", args,
                    flags=("--measure", "--json"), usage=_USAGE)
    if rc is not None:
        return rc
    try:
        value = measure() if "--measure" in args else view()
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print("helm storage-matrix: measurement failed: %s" % exc, file=sys.stderr)
        return 2
    if "--json" in args:
        if "--measure" in args:
            value = view()
        print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))
        return 0 if value.get("state") == "ok" and \
            value.get("status") == "complete" and not value.get("stale") else 1
    return _print_human(view() if "--measure" in args else value)
