#!/usr/bin/env python3
"""Count retired Herdr native wake/inject evidence for an MC session."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Iterable

TURN_STARTER = "take a turn: an action-required comms record is pending for you"

NATIVE_INJECT_PATTERNS = {
    "native_wake_dropped": re.compile(
        r"action-required wake dropped: pane runtime channel full/closed",
        re.IGNORECASE,
    ),
    "native_wake_sent": re.compile(
        r"(legacy|native|keystroke|action-required).{0,80}"
        r"(wake|inject).{0,80}(sent|fired|delivered|typed|submitted)",
        re.IGNORECASE,
    ),
    "turn_starter_text_seen": re.compile(re.escape(TURN_STARTER), re.IGNORECASE),
    "runtime_send_text_wake": re.compile(
        r"(pane\.send_text|pane\.send_keys|send[_-]?input|try_send_bytes).{0,120}"
        r"(wake|turn-starter|action-required)",
        re.IGNORECASE,
    ),
}

SUPPRESSED_PATTERNS = {
    "native_wake_suppressed": re.compile(
        r"keystroke wake suppressed|injection path retired; legacy flag off",
        re.IGNORECASE,
    ),
}

CONFIG_TRUE = re.compile(r"^\s*legacy_injection_wake\s*=\s*true\s*(?:#.*)?$")


def infer_session_dir() -> Path:
    for env_name in ("HERDR_SOCKET_PATH", "MC_SESSION_SOCKET"):
        value = os.environ.get(env_name)
        if value:
            return Path(value).expanduser().resolve().parent
    session = os.environ.get("MC_SESSION") or "mc-headful"
    return Path.home() / ".config" / "herdr" / "sessions" / session


def existing(paths: Iterable[Path]) -> list[Path]:
    return [path for path in paths if path.exists()]


def default_scan_files(session_dir: Path, include_session_history: bool) -> list[Path]:
    files = existing(
        [
            session_dir / "herdr-server.log",
            session_dir / "herdr-client.log",
            session_dir / "comms.log",
            session_dir / "mc-receipts.log",
            Path.home() / ".config" / "herdr" / "mc-guard-ledger.jsonl",
        ]
    )
    guardrail_dir = session_dir / "guardrail"
    if guardrail_dir.is_dir():
        files.extend(sorted(guardrail_dir.glob("*.speclog")))
    if include_session_history:
        files.extend(existing([session_dir / "session-history.json"]))
    return files


def default_config_files(session_dir: Path) -> list[Path]:
    return existing(
        [
            Path.home() / ".config" / "herdr" / "config.toml",
            session_dir / "config.toml",
        ]
    )


def record_match(matches: list[dict], path: Path, line_no: int, kind: str, line: str) -> None:
    matches.append(
        {
            "file": str(path),
            "line": line_no,
            "kind": kind,
            "sample": line.rstrip("\n")[:240],
        }
    )


def scan_text_file(path: Path, counts: dict, matches: list[dict]) -> None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_no, line in enumerate(handle, start=1):
                for kind, pattern in NATIVE_INJECT_PATTERNS.items():
                    if pattern.search(line):
                        counts["native_inject_attempts"] += 1
                        counts["native_inject_kinds"][kind] = (
                            counts["native_inject_kinds"].get(kind, 0) + 1
                        )
                        record_match(matches, path, line_no, kind, line)
                for kind, pattern in SUPPRESSED_PATTERNS.items():
                    if pattern.search(line):
                        counts["native_inject_suppressed"] += 1
                        counts["native_inject_kinds"][kind] = (
                            counts["native_inject_kinds"].get(kind, 0) + 1
                        )
    except OSError as err:
        counts["scan_errors"].append({"file": str(path), "error": str(err)})


def scan_comms_file(path: Path, counts: dict) -> None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                record = row.get("Record")
                if isinstance(record, dict):
                    counts["records"] += 1
                    if record.get("priority") == "ActionRequired":
                        counts["action_required_records"] += 1
                if "Ack" in row:
                    counts["acks"] += 1
    except OSError as err:
        counts["scan_errors"].append({"file": str(path), "error": str(err)})


def guard_entry_is_native_inject(entry: dict) -> bool:
    haystack = " ".join(
        str(entry.get(key, "")) for key in ("hook", "verdict", "detail")
    ).lower()
    if "dispatch-coordination-snapshot" in haystack:
        return False
    return (
        "native" in haystack
        or "legacy_injection_wake" in haystack
        or "keystroke wake" in haystack
        or "turn-starter" in haystack
        or "action-required wake" in haystack
    ) and "inject" in haystack


def scan_guard_ledger(path: Path, counts: dict, matches: list[dict]) -> None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_no, line in enumerate(handle, start=1):
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("verdict") != "inject":
                    continue
                if guard_entry_is_native_inject(entry):
                    counts["native_inject_attempts"] += 1
                    counts["native_inject_kinds"]["guard_native_inject"] = (
                        counts["native_inject_kinds"].get("guard_native_inject", 0) + 1
                    )
                    record_match(matches, path, line_no, "guard_native_inject", line)
                else:
                    counts["guard_context_injections_ignored"] += 1
    except OSError as err:
        counts["scan_errors"].append({"file": str(path), "error": str(err)})


def scan_config(path: Path, counts: dict, matches: list[dict]) -> None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_no, line in enumerate(handle, start=1):
                if CONFIG_TRUE.search(line):
                    counts["legacy_injection_enabled_configs"] += 1
                    record_match(matches, path, line_no, "legacy_injection_wake_true", line)
    except OSError as err:
        counts["scan_errors"].append({"file": str(path), "error": str(err)})


def run(args: argparse.Namespace) -> dict:
    session_dir = args.session_dir.expanduser().resolve()
    scan_files = [path.expanduser().resolve() for path in args.file]
    if not scan_files:
        scan_files = default_scan_files(session_dir, args.include_session_history)

    config_files = [path.expanduser().resolve() for path in args.config]
    if not config_files:
        config_files = default_config_files(session_dir)

    counts = {
        "session_dir": str(session_dir),
        "files_scanned": [str(path) for path in scan_files],
        "config_files_scanned": [str(path) for path in config_files],
        "records": 0,
        "action_required_records": 0,
        "acks": 0,
        "native_inject_attempts": 0,
        "native_inject_suppressed": 0,
        "native_inject_kinds": {},
        "legacy_injection_enabled_configs": 0,
        "guard_context_injections_ignored": 0,
        "scan_errors": [],
        "matches": [],
    }

    for path in scan_files:
        if path.name == "comms.log":
            scan_comms_file(path, counts)
        if path.name == "mc-guard-ledger.jsonl":
            scan_guard_ledger(path, counts, counts["matches"])
        else:
            scan_text_file(path, counts, counts["matches"])

    for path in config_files:
        scan_config(path, counts, counts["matches"])

    counts["status"] = (
        "PASS"
        if counts["native_inject_attempts"] == 0
        and counts["legacy_injection_enabled_configs"] == 0
        else "FAIL"
    )
    return counts


def print_text(result: dict) -> None:
    print("zero-inject counter")
    for key in (
        "status",
        "session_dir",
        "records",
        "action_required_records",
        "acks",
        "native_inject_attempts",
        "native_inject_suppressed",
        "legacy_injection_enabled_configs",
        "guard_context_injections_ignored",
    ):
        print(f"{key}: {result[key]}")
    print(f"files_scanned: {len(result['files_scanned'])}")
    if result["native_inject_kinds"]:
        print("native_inject_kinds:")
        for kind, count in sorted(result["native_inject_kinds"].items()):
            print(f"  {kind}: {count}")
    if result["matches"]:
        print("matches:")
        for match in result["matches"][:20]:
            print(
                f"  {match['file']}:{match['line']} "
                f"{match['kind']}: {match['sample']}"
            )
        overflow = len(result["matches"]) - 20
        if overflow > 0:
            print(f"  ... {overflow} more")
    if result["scan_errors"]:
        print("scan_errors:")
        for error in result["scan_errors"]:
            print(f"  {error['file']}: {error['error']}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure retired Herdr native wake/inject use for an MC session."
    )
    parser.add_argument(
        "--session-dir",
        type=Path,
        default=infer_session_dir(),
        help="Herdr session directory; defaults to HERDR_SOCKET_PATH parent or mc-headful.",
    )
    parser.add_argument(
        "--file",
        type=Path,
        action="append",
        default=[],
        help="Specific log file to scan; repeatable. Defaults to MC session logs.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        action="append",
        default=[],
        help="Specific config file to scan for legacy_injection_wake=true.",
    )
    parser.add_argument(
        "--include-session-history",
        action="store_true",
        help="Also scan session-history.json. This can be large and noisy.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print JSON instead of text.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    result = run(args)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print_text(result)
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
