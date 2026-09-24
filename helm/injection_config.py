"""Point-in-time config identity joined to injection-weight samples.

This opt-in read model deliberately stays out of the roster projection: reading
injection telemetry or the transcript catalog on a presence poll would turn a
lightweight roster read into a private, expensive config scan.
"""
import os

from . import injection_schema, runtime_config


INJECTION_USAGE = "helm configs injection [--seat SEAT] [--session SID]"
_SAMPLE_CAP = 24
_EXACT = tuple((version, "v%d" % version)
               for version in injection_schema.EXACT_VERSIONS)


def _field(candidates):
    """One agreed authoritative value, or an explicit unknown/conflict."""
    rows = [(str(value), source) for value, source in candidates if value]
    values = []
    for value, _source in rows:
        if value not in values:
            values.append(value)
    sources = []
    for _value, source in rows:
        if source not in sources:
            sources.append(source)
    if len(values) == 1:
        return {"value": values[0], "sources": sources}
    if len(values) > 1:
        return {"value": None, "sources": sources, "conflict": values}
    return {"value": None, "sources": []}


def _catalog_transcript_home(row):
    """Transcript home proved by a catalog path, never runtime config authority."""
    path = row.get("p") if isinstance(row, dict) else None
    harness = row.get("h") if isinstance(row, dict) else None
    if not path or not harness:
        return None
    try:
        from . import configs
        path = os.path.realpath(os.path.expanduser(path))
        cur = os.path.dirname(path)
        while cur and cur != os.path.dirname(cur):
            if configs.harness_for(cur) == harness:
                rel = os.path.relpath(path, cur)
                top = rel.split(os.sep, 1)[0]
                if (harness == "claude" and top == "projects") or \
                        (harness == "codex" and top == "sessions"):
                    return cur
            cur = os.path.dirname(cur)
    except Exception:
        pass
    return None


def _catalog_rows(session):
    if not session:
        return [], None
    try:
        from . import transcripts
        rows = [row for row in transcripts.get_catalog()["rows"]
                if isinstance(row, dict) and row.get("i") == session]
        return rows, "catalog-ambiguous" if len(rows) > 1 else None
    except Exception:
        return [], "catalog"


def _row_sessions(row):
    if not isinstance(row, dict):
        return set()
    out = {str(s) for s in row.get("sessions") or [] if s}
    if row.get("session"):
        out.add(str(row["session"]))
    entries = row.get("runtime_sessions")
    if isinstance(entries, dict):
        out.update(str(s) for s in entries)
    return out


def _roster_row(seat, session):
    """Canonical backend roster binding plus a read-truth verdict."""
    try:
        from . import seats
        roster, failed = seats.roster_acquired()
    except Exception:
        return None, None, "roster"
    if failed:
        return None, None, "roster"
    if seat:
        matches = [name for name in roster
                   if str(name).casefold() == str(seat).casefold()]
        if len(matches) > 1:
            return None, None, "roster-ambiguous"
        if matches:
            name = matches[0]
            row = roster[name]
            if not session or str(session) in _row_sessions(row):
                return seats._seat_label(name), row, None
    if session:
        matches = [(name, row) for name, row in roster.items()
                   if str(session) in _row_sessions(row)]
        if len(matches) > 1:
            return None, None, "roster-ambiguous"
        if matches:
            name, row = matches[0]
            return seats._seat_label(name), row, None
    return None, None, None


def _runtime_harness(row, session):
    if not isinstance(row, dict) or not session:
        return None
    try:
        from . import seats
        runtime, verified = seats.runtime_for_session(row, session)
    except Exception:
        return None
    return runtime.get("agent_harness") if verified and isinstance(runtime, dict) else None


def _rows():
    try:
        from . import inject
        return inject._ledger_rows(strict=True), None
    except Exception:
        return [], "ledger"


def _inject_source(row, field):
    if not isinstance(row, dict) or not injection_schema.valid_exact(row):
        return None
    version = row.get("v")
    if field != "config_home":
        return "inject-v%d" % version
    source = (row.get("context_sources") or {}).get("config_home")
    if version == injection_schema.V3:
        return ("inject-v3-agent-default" if
                source == runtime_config.DEFAULT_SOURCE else
                "inject-v3-explicit-env")
    return "inject-v2-explicit-env"


def _summary(rows, kind):
    count = len(rows)
    if kind == "exact":
        total = sum(row["rendered_bytes"] for row in rows)
        return {"state": "observed" if count else "unknown", "count": count,
                "rendered_bytes": total,
                "average_rendered_bytes": round(total / count, 1) if count else None,
                "samples": rows[-_SAMPLE_CAP:]}
    total = sum(row["approx_characters"] for row in rows)
    return {"state": "partial" if count else "unknown", "count": count,
            "approx_characters": total,
            "average_approx_characters": round(total / count, 1) if count else None,
            "samples": rows[-_SAMPLE_CAP:]}


def _exact_groups(cohorts, unassigned, current_key):
    selected = cohorts.get(current_key, {"rows": []})["rows"] \
        if current_key else []
    other = []
    for key, cohort in cohorts.items():
        if key == current_key:
            continue
        item = {"context": cohort["context"]}
        item.update(_summary(cohort["rows"], "exact"))
        other.append(item)
    return (_summary(selected, "exact"), other,
            _summary(unassigned, "exact"))


def _samples(rows, session, current):
    cohorts = {version: {} for version, _prefix in _EXACT}
    unassigned = {version: [] for version, _prefix in _EXACT}
    approx = []
    if not session:
        rows = []
    for row in rows:
        if not isinstance(row, dict) or row.get("session") != session:
            continue
        if injection_schema.valid_exact(row):
            version = row["v"]
            sample = row["sample"]
            observed = {"ts": row.get("ts"), "turn": row.get("turn"),
                        "rendered_bytes": sample["rendered_bytes"],
                        "lane_bytes": {lane: sample["lane_bytes"][lane]
                                       for lane in injection_schema.V2_LANES},
                        "silent": row.get("silent") is True}
            if row.get("context_unavailable"):
                observed["context_unavailable"] = row["context_unavailable"]
            context = injection_schema.config_context(row)
            if context is None:
                unassigned[version].append(observed)
                continue
            key = tuple(context[name] for name in
                        ("harness", "cwd", "config_home"))
            cohorts[version].setdefault(
                key, {"context": context, "rows": []})["rows"].append(observed)
        elif row.get("v") == 1 and isinstance(row.get("bytes"), dict):
            chars = {lane: row["bytes"].get(lane)
                     for lane in injection_schema.V2_LANES
                     if type(row["bytes"].get(lane)) is int
                     and row["bytes"][lane] >= 0}
            approx.append({"ts": row.get("ts"), "turn": row.get("turn"),
                           "approx_characters": sum(chars.values()),
                           "lane_characters": chars,
                           "silent": row.get("silent") is True})
    current_key = tuple(current.get(name) for name in
                        ("harness", "cwd", "config_home")) \
        if all(current.get(name) for name in
               ("harness", "cwd", "config_home")) else None
    out = {"v1_approx": _summary(approx, "v1")}
    for version, prefix in _EXACT:
        selected, other, loose = _exact_groups(
            cohorts[version], unassigned[version], current_key)
        out.update({prefix + "_utf8": selected,
                    prefix + "_other_cohorts": other,
                    prefix + "_unassigned": loose})
    return out


def unknown_view(seat=None, session=None, unavailable="observation"):
    """Stable empty model shared by source failures and the web adapter."""
    seat = str(seat or "").strip() or None
    session = str(session or "").strip() or None
    fields = {name: {"value": None, "sources": []} for name in (
        "seat", "session", "cwd", "harness", "transcript_home", "config_home")}
    samples = _samples([], None, {})
    return {"state": "unknown", "requested": {"seat": seat, "session": session},
            "target": fields, "annotations": {"catalog_cwd": []},
            "missing": list(fields), "conflicts": [],
            "unavailable": [unavailable] if unavailable else [], "samples": samples}


def _latest_exact(rows, field):
    """Newest authoritative field; failed newer samples stay unassigned only."""
    for row in reversed(rows):
        if not injection_schema.valid_exact(row):
            continue
        value = (injection_schema.config_home(row) if field == "config_home"
                 else injection_schema.context_value(row, field))
        if value is not None:
            return value, row
    return None, {}


def view(seat=None, session=None, rows=None):
    """Observed/partial/unknown Config view for one roster seat/session."""
    seat = str(seat or "").strip() or None
    session = str(session or "").strip() or None
    requested = {"seat": seat, "session": session}
    roster_seat, roster, roster_error = _roster_row(seat, session)
    if roster and not session:
        session = roster.get("session")
    catalog, catalog_error = _catalog_rows(session)
    if rows is None:
        all_rows, ledger_error = _rows()
    else:
        all_rows, ledger_error = rows, None
    matching = [row for row in all_rows if session and isinstance(row, dict)
                and row.get("session") == session]
    injected = {field: _latest_exact(matching, field) for field in
                ("session", "cwd", "harness", "config_home")}
    current_roster = roster if roster and roster.get("session") == session else None
    roster_source = "roster-current" if current_roster else \
        "roster-history" if roster else None

    fields = {
        "seat": _field([(roster_seat, roster_source)]),
        "session": _field(
            [(session if roster_source == "roster-history" else
              current_roster.get("session") if current_roster else None,
              roster_source)]
            + [(row.get("i"), "catalog-id") for row in catalog]
            + [(injected["session"][0],
                _inject_source(injected["session"][1], "session"))]),
        "cwd": _field([
            (current_roster.get("cwd") if current_roster else None,
             "roster-current"),
            (injected["cwd"][0],
             _inject_source(injected["cwd"][1], "cwd"))]),
        "harness": _field(
            [(_runtime_harness(roster, session), "roster-verified-runtime")]
            + [(row.get("h"), "catalog-root") for row in catalog]
            + [(injected["harness"][0],
                _inject_source(injected["harness"][1], "harness"))]),
        "transcript_home": _field([
            (_catalog_transcript_home(row), "catalog-path") for row in catalog]),
        "config_home": _field([
            (injected["config_home"][0],
             _inject_source(injected["config_home"][1], "config_home"))]),
    }
    current = {name: fields[name]["value"] for name in
               ("harness", "cwd", "config_home")}
    samples = _samples(all_rows, fields["session"]["value"], current)
    required = ["seat", "session", "cwd", "harness", "config_home"]
    if fields["harness"]["value"] in ("claude", "codex"):
        required.append("transcript_home")
    missing = [name for name in required if fields[name]["value"] is None]
    conflicts = [name for name, field in fields.items() if field.get("conflict")]
    unavailable = [name for name in (roster_error, catalog_error, ledger_error) if name]
    exact_current = [prefix + "_utf8" for _version, prefix in _EXACT]
    exact_unassigned = [prefix + "_unassigned" for _version, prefix in _EXACT]
    exact_other = [prefix + "_other_cohorts" for _version, prefix in _EXACT]
    complete = not missing and not conflicts and not unavailable \
        and any(samples[name]["count"] > 0 for name in exact_current)
    any_evidence = any(field["value"] is not None or field.get("conflict")
                       for field in fields.values()) \
        or any(samples[name]["count"] > 0 for name in
               exact_current + exact_unassigned + ["v1_approx"]) \
        or any(samples[name] for name in exact_other)
    state = "observed" if complete else "partial" if any_evidence else "unknown"
    catalog_cwd = []
    for row in catalog:
        cwd = row.get("cwd")
        if cwd and cwd not in catalog_cwd:
            catalog_cwd.append(cwd)
    return {"state": state, "requested": requested, "target": fields,
            "annotations": {"catalog_cwd": catalog_cwd},
            "missing": missing, "conflicts": conflicts,
            "unavailable": unavailable, "samples": samples}
