"""`helm seat remint` and the launch drift `helm seat doctor` prints.

THE FAILURE THIS ENDS (2026-09-28). The gemini seat's launch.sh, last written
three days earlier, carried `--model gemini-3.6-flash-high` and the same
subagent pin while the catalog, the seat's proxy config and its own
settings.json all said 3.8, and its spawn.json named no model at all. Started
from that script the seat came up on a route the proxy had no credential for
and stayed dead until the owner typed /model by hand. A sweep of the fleet
found ten more seats whose scripts launch a model the catalog does not name.
launch.sh is re-minted only by add, launch, spawn and resume, so a catalog
change reaches a seat only when one of those happens to run, and nothing said
when one had not.

ONE READING, TWO SURFACES. `launch_drift` asks what a FRESH MINT would write
for a seat now and compares it with the script on disk: the model, the
subagent pin, and every environment word of the launch line. `seat doctor`
prints a DRIFT row per drifted seat and fails its exit status; `seat remint`
prints the same rows as a dry-run plan and, with --apply, re-mints through
`_write_launch_assets` (the refresh launch/resume/add already run) and
`regenerate_proxy_config` (the path `doctor --ensure` already runs). There is
no second mint here, and nothing is started or stopped: a running pane keeps
its model until it is resumed, and a running proxy takes a regenerated config
at its next `seat up` or ensure pass.

THE FRESH MINT IS ASKED OF THE MINTER. The expected line is `launch_line`
itself, fed the room, room provenance and --multi shape the script on disk
carries (the same three reads a resume makes) and the model
`_persisted_model` answers, so this reading cannot disagree with the re-mint
it predicts.

A DEFAULT IS NOT A CHOICE. spawn.json's `model_source` says whether a
persisted model was the operator's `--model` ("explicit", which outranks the
catalog) or the family default at that moment ("default", kept as history,
which the catalog outranks). A record written before the marker existed
cannot say which it was: helm has no record of what the catalog said when it
was written, so a model that matches an OLD default cannot be told apart from
a deliberate pin. Such a model is KEPT, as it always was, and reported as
"persisted, source unknown" wherever it disagrees with the catalog, until the
operator settles it: `--follow-catalog` records it as a default,
`--keep-persisted` as a choice. Only that decision writes spawn.json.
"""
import os
import re
import shlex
import sys

from . import pk, seat

EXPLICIT = "explicit"
DEFAULT = "default"
SOURCE_UNKNOWN = "persisted, source unknown"

CURRENT, DRIFT, UNKNOWN, UNMINTED = "CURRENT", "DRIFT", "UNKNOWN", "UNMINTED"

USAGE = ("seat remint <seat>|--all [--apply] "
         "[--follow-catalog|--keep-persisted]")

_ENV_WORD = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
_SUBAGENT = "CLAUDE_CODE_SUBAGENT_MODEL"
_CONFIG_ERRORS = (IndexError, KeyError, OSError, TypeError, ValueError)


def launch_fields(text):
    """{"model", "subagent", "env"} of the `claude` command a launch text
    runs, or None when it names none.

    Read shell-aware, with the nested re-split `_model_from_launch_text`
    documents: `_launch_owner` hands the whole launch line to its supervisor
    as ONE quoted argument, so the command is inside a single token of the
    script. The same reader parses the script on disk and the line
    `launch_line` renders, so the two sides are compared in one shape.
    `env` is the NAME=value words between `env` and `claude`; the subagent pin
    is lifted out of it because it is its own field."""
    try:
        tokens = shlex.split(text, comments=True)
    except ValueError:
        return None
    runs = [tokens]
    for token in tokens:
        if "--model" in token and token != "--model":
            try:
                runs.append(shlex.split(token))
            except ValueError:
                pass
    for run in reversed(runs):
        starts = [i for i, t in enumerate(run)
                  if t == "claude" and "--model" in run[i + 1:]]
        if not starts:
            continue
        c = starts[-1]
        tail = run[c + 1:]
        m = len(tail) - 1 - tail[::-1].index("--model")
        env_at = max([i for i in range(c) if run[i] == "env"], default=-1)
        env = {}
        for word in run[env_at + 1:c]:
            hit = _ENV_WORD.match(word)
            if hit:
                env[hit.group(1)] = hit.group(2)
        return {"model": tail[m + 1] if m + 1 < len(tail) else None,
                "subagent": env.pop(_SUBAGENT, None), "env": env}
    return None


def persisted_choice(d, seat_name):
    """(model, source, unread) of the model spawn.json persists for this
    seat, source EXPLICIT, DEFAULT or SOURCE_UNKNOWN; (None, None, None) when
    it persists none. The same record `_persisted_model` reads, classified
    the same way, but read STRICTLY: a spawn.json that exists and cannot be
    read, or is not an object, is (None, None, why), because a choice helm
    could not read is not an absent choice, and reading it as one re-mints
    the seat off the model it chose."""
    rec, unread = seat._spawn_record_read(d)
    if unread:
        return None, None, unread
    if not rec or rec.get("seat") != seat_name or not rec.get("model"):
        return None, None, None
    source = rec.get("model_source")
    return rec["model"], (source if source in (EXPLICIT, DEFAULT)
                          else SOURCE_UNKNOWN), None


def launch_drift(family, seat_name, decide=None):
    """What a fresh mint of this seat would write now, against its launch.sh.

    Returns a row: `state` CURRENT, DRIFT, UNKNOWN (with `why`) or UNMINTED
    (no launch.sh at all — nothing drifts; an unreadable launch.sh or
    spawn.json is UNKNOWN, never a verdict), `fields` [(name, on disk, fresh)]
    for every differing field (`model`, `subagent`, then each env word by
    name), `want_from` naming what decides the model ("catalog", "explicit
    choice" or SOURCE_UNKNOWN), and `unsettled` — a legacy persisted model
    that outranks a DIFFERENT catalog model, which is DRIFT on its own
    because it is exactly a seat launching a model the catalog does not name.
    `decide` (DEFAULT or EXPLICIT) reads a legacy record as if the operator
    had already settled it that way, which is how the dry run shows the
    effect of `--follow-catalog`/`--keep-persisted` before writing."""
    from .seat_catalog import instance_launch_model
    from .seat_launch_assets import _launch_identity
    d = seat._instance_dir(family, seat_name)
    launch = os.path.join(d, "launch.sh")
    persisted, source, unread = persisted_choice(d, seat_name)
    decided = decide if source == SOURCE_UNKNOWN and decide else None
    effective = decided or source
    binding = persisted if effective in (EXPLICIT, SOURCE_UNKNOWN) else None
    catalog = instance_launch_model(seat.FAMILIES[family], seat_name)
    row = {"family": family, "seat": seat_name, "dir": d,
           "launch_sh": launch, "state": UNKNOWN, "why": "", "fields": [],
           "persisted": persisted, "source": source, "decided": decided,
           "catalog": catalog, "unsettled": False,
           "want_from": ("catalog" if binding is None
                         else "explicit choice" if effective == EXPLICIT
                         else SOURCE_UNKNOWN)}
    try:
        with open(launch) as f:
            text = f.read()
    except FileNotFoundError:
        row["state"] = UNMINTED
        return row
    except (OSError, ValueError) as exc:
        row["why"] = "launch.sh unreadable (%s)" % type(exc).__name__
        return row
    if unread:
        row["why"] = unread
        return row
    have = launch_fields(text)
    if have is None:
        row["why"] = "launch.sh names no `claude --model` command"
        return row
    identity, err = _launch_identity(seat_name)
    if err:
        row["why"] = "launch identity unresolved: %s" % err
        return row
    room, room_source = seat._homing_from_launch(launch)
    try:
        want = launch_fields(seat.launch_line(
            family, binding, room, seat_name, room_source=room_source,
            multi=seat._multi_from_launch(launch), identity=identity))
    except (KeyError, TypeError, ValueError) as exc:
        row["why"] = "a fresh mint cannot render: %s" % exc
        return row
    # THE ROOM AS IT IS READ, NOT AS IT IS SPELLED. A spawn with --room
    # stamps HELM_CHAT_ROOM_SOURCE=explicit, and the fresh mint above spells
    # the same room with no stamp (`_homing_from_launch` keeps only
    # "derived"); the child reads both as explicit
    # (`seats_identity.resolve_homing`), so a fresh spawn is not DRIFT.
    for env in (have["env"], want["env"]):
        if env.get("HELM_CHAT_ROOM") \
                and env.get("HELM_CHAT_ROOM_SOURCE") == "explicit":
            del env["HELM_CHAT_ROOM_SOURCE"]
    fields = [(name, have[name], want[name]) for name in ("model", "subagent")
              if have[name] != want[name]]
    for key in sorted(set(have["env"]) | set(want["env"])):
        if have["env"].get(key) != want["env"].get(key):
            fields.append((key, have["env"].get(key), want["env"].get(key)))
    row["fields"] = fields
    row["unsettled"] = (effective == SOURCE_UNKNOWN and persisted != catalog)
    row["state"] = DRIFT if fields or row["unsettled"] else CURRENT
    return row


def _field_text(row):
    parts = []
    for name, have, want in row["fields"]:
        part = "%s %s -> %s" % (name, have or "(absent)", want or "(absent)")
        if name == "model":
            part += " (%s)" % row["want_from"]
        parts.append(part)
    if row["unsettled"]:
        parts.append("model %s is %s, and outranks the catalog's %s"
                     % (row["persisted"], SOURCE_UNKNOWN, row["catalog"]))
    return "; ".join(parts)


def _cure_text(row):
    s = row["seat"]
    cures = []
    if row["fields"]:
        cures.append("`helm seat remint %s --apply` re-mints launch.sh" % s)
    if row["unsettled"]:
        cures.append("`helm seat remint %s --follow-catalog --apply` follows "
                     "the catalog, `--keep-persisted --apply` keeps it as a "
                     "choice" % s)
    return "; ".join(cures)


def _launch_seats():
    """(family, seat) for every minted proxy seat that has a launch.sh — the
    one enumeration doctor walks (`_minted_seats`), narrowed to a script."""
    for family, s in seat._minted_seats():
        if os.path.lexists(os.path.join(seat._instance_dir(family, s),
                                        "launch.sh")):
            yield family, s


def doctor_rows():
    """[(failed, line)] for `seat doctor`: one row per DRIFT seat
    (failed=True) and per seat whose drift could not be read (UNKNOWN,
    failed=False — an unread script is never a verdict). CURRENT seats print
    nothing. Read-only."""
    out = []
    for family, s in _launch_seats():
        row = launch_drift(family, s)
        if row["state"] == DRIFT:
            out.append((True, "launch drift: %-9s DRIFT — %s — %s (no process "
                        "is started or stopped; a running pane keeps its "
                        "model until it is resumed)"
                        % (s, _field_text(row), _cure_text(row))))
        elif row["state"] == UNKNOWN:
            out.append((False, "launch drift: %-9s UNKNOWN — %s"
                        % (s, row["why"])))
    return out


def _settle(d, seat_name, model, source):
    """Record a legacy persisted model's source, under the caller's lock.
    Re-read first: a record that changed since the plan is left alone."""
    rec = seat._spawn_record(d)
    if not rec or rec.get("seat") != seat_name or rec.get("model") != model \
            or rec.get("model_source") in (EXPLICIT, DEFAULT):
        return False
    rec["model_source"] = source
    pk.write_json(seat._spawn_path(d), rec)
    return True


def _config_plan(family, s):
    """(path, changed, error) for the seat's proxy config; path None when it
    has none of its own."""
    path = os.path.join(seat._proxy_home(family, s), "config.yaml")
    if not os.path.exists(path):
        return None, False, None
    try:
        return path, seat.proxy_config_plan(path, family, s)["changed"], None
    except _CONFIG_ERRORS as exc:
        return path, False, str(exc)


def _remint_one(family, s, apply, decide):
    """Plan (and with `apply`, perform) one seat's re-mint; returns rc."""
    row = launch_drift(family, s, decide=decide)
    if row["state"] == UNMINTED:
        print("helm seat remint: %s has no launch.sh — nothing to re-mint "
              "(`helm seat launch %s` mints one)" % (s, s))
        return 0
    if row["state"] == UNKNOWN:
        print("helm seat remint: %s UNKNOWN — %s; nothing written"
              % (s, row["why"]))
        return 1
    cfg, cfg_changed, cfg_error = _config_plan(family, s)
    settle = row["decided"]
    rewrite = bool(row["fields"])
    verb = "re-minting" if apply else "would re-mint"
    if row["state"] == CURRENT and not settle:
        print("helm seat remint: %s CURRENT — launch.sh matches a fresh mint "
              "(model %s, %s)" % (s, row["catalog"] if row["want_from"] ==
                                  "catalog" else row["persisted"],
                                  row["want_from"]))
    else:
        print("helm seat remint: %s %s — %s"
              % (s, row["state"], _field_text(row) or "the script matches"))
    if settle:
        print("  %s spawn.json: %s recorded as %s" % (
            "writing" if apply else "would write", row["persisted"],
            "the family default at spawn time (follows the catalog)"
            if settle == DEFAULT else "an explicit choice (outranks the "
            "catalog)"))
    elif row["unsettled"]:
        print("  " + _cure_text(row))
    if rewrite:
        print("  %s %s" % (verb, row["launch_sh"]))
    if cfg_error:
        print("  proxy config %s: desired state unreadable — %s; left as is"
              % (cfg, cfg_error))
    elif cfg_changed:
        print("  %s proxy config %s (generated policy differs)"
              % ("regenerating" if apply else "would regenerate", cfg))
    if not (rewrite or settle or cfg_changed):
        return 1 if cfg_error else 0
    if not apply:
        print("  dry run — nothing written; --apply writes it (no process is "
              "started or stopped)")
        return 1 if cfg_error else 0
    d, launch = row["dir"], row["launch_sh"]
    with seat._seat_lifecycle_lock(d):
        # the door every mutating verb knocks on before its first write: a
        # spawn releases this lock while its child starts, and only its
        # PENDING attempt keeps a re-mint from landing under that child
        if seat._refuse_in_flight_spawn(d):
            return 1
        # the plan's strict read, again under the lock: the model the re-mint
        # carries comes from this record, and an unread one is not "none"
        _rec, unread = seat._spawn_record_read(d)
        if unread:
            print("helm seat remint: %s UNKNOWN — %s; nothing written"
                  % (s, unread), file=sys.stderr)
            return 1
        if settle and not _settle(d, s, row["persisted"], settle):
            print("helm seat remint: %s's spawn.json changed since it was "
                  "read; nothing written — re-run" % s, file=sys.stderr)
            return 1
        if rewrite or settle:
            was = seat._launch_snapshot(launch)
            if was[0] == "unknown":
                print("helm seat remint: refusing to re-mint unreadable "
                      "launch script %s" % launch, file=sys.stderr)
                return 1
            room, room_source = seat._homing_from_launch(launch)
            try:
                refused = seat._write_launch_assets(
                    family, d, room, s, room_source=room_source,
                    multi=seat._multi_from_launch(launch),
                    model=seat._persisted_model(d, s)) \
                    is seat._SEAT_SURFACE_REFUSED
            except OSError as exc:
                seat._restore_launch(launch, was)
                print("helm seat remint: %s re-mint failed: %s" % (s, exc),
                      file=sys.stderr)
                return 1
            if refused:
                seat._restore_launch(launch, was)
                return 1
        if cfg_changed:
            _changed, err = seat.regenerate_proxy_config(cfg, family, s)
            if err:
                print("helm seat remint: %s proxy config not regenerated: %s"
                      % (s, err), file=sys.stderr)
                return 1
    after = launch_drift(family, s)
    if after["fields"]:
        print("helm seat remint: %s still DRIFT after the re-mint — %s"
              % (s, _field_text(after)), file=sys.stderr)
        return 1
    print("  done — a running pane keeps its model until `helm seat resume "
          "%s`" % s)
    return 0


def cmd_remint(rest):
    """seat remint <seat>|--all [--apply] [--follow-catalog|--keep-persisted]
    — re-mint launch.sh (and the proxy config) from the catalog, or from the
    seat's explicit choice, without launching anything. Dry run by default."""
    from .cli import guard_tail
    rc = guard_tail("helm seat remint", [a for a in rest if a.startswith("-")],
                    flags=("--all", "--apply", "--follow-catalog",
                           "--keep-persisted"), usage=USAGE)
    if rc is not None:
        return rc
    names = [a for a in rest if not a.startswith("-")]
    every = "--all" in rest
    if every == bool(names) or len(names) > 1:
        print("usage: helm " + USAGE, file=sys.stderr)
        return 2
    follow, keep = "--follow-catalog" in rest, "--keep-persisted" in rest
    if follow and keep:
        print("helm seat remint: --follow-catalog and --keep-persisted answer "
              "the same question two ways; pass one", file=sys.stderr)
        return 2
    decide = DEFAULT if follow else EXPLICIT if keep else None
    if every:
        targets = list(_launch_seats())
    else:
        family, err = seat._seat_family(names[0])
        if err:
            print("helm seat: " + seat._unknown_seat_reason(names[0], err),
                  file=sys.stderr)
            return 2
        if family not in seat.FAMILIES:
            print("helm seat remint: %s is a %s seat — it has no launch.sh "
                  "to re-mint" % (names[0], family), file=sys.stderr)
            return 2
        targets = [(family, names[0])]
    worst = 0
    for family, s in targets:
        worst = max(worst, _remint_one(family, s, "--apply" in rest, decide))
    return worst
