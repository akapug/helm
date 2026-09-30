"""The DESIRED-DOWN record: `helm seat down <seat>` survives its own supervisor.

THE FAILURE THIS ENDS. An operator retired a seat and ran `helm seat down` on
it. The proxy stopped, and the */3 `helm seat doctor --ensure` cron RESPAWNED
it on its next pass; proxywatch then canaried the respawned proxy and raised
FAMILY-DARK for a family nobody wanted running. Nothing was wrong with either
supervisor: "minted" (a config.yaml in the seat's proxy home) was the only
fact the supervise surface read, and a minted proxy that is not running is
exactly the silent-starvation case the watchdog exists to heal. The verb said
down; nothing recorded that it had been said, so the supervisor disagreed
three minutes later.

ONE RECORD, BESIDE THE PROXY IT GOVERNS. `<proxy home>/desired-down.json`,
where the proxy home is `seat_paths._proxy_home` — the directory that already
owns "a seat's proxy fate" (config.yaml, proxy.pid, proxy.log, the lock).
Instance 1 keeps the family dir and every other instance its own
`instances/<seat>/`, so one seat's record can never speak for a sibling. The
bytes follow the per-seat register's convention (`spawn.json`): one JSON
object written whole by `pk.atomic_write`, private (0600), read back through
`pk.open_regular`, the regular-file door every store reader uses:

    {"v": 1, "seat": S, "family": F, "by": NAME|"UNKNOWN", "at": TS,
     "reason": R|null}

`by` is the writer's DECLARED seat name (HELM_CHAT_NAME through its one
validated seam) or the literal UNKNOWN; `at` is `pk.now_ts()`.

WHO WRITES IT AND WHO CLEARS IT, and this is the whole design. Only the
operator's own verbs touch it: `helm seat down` records it (before it stops
the proxy), and `helm seat up`, `helm seat resume <seat>` and `helm seat spawn`
clear it, because each of those is the operator saying "run it". Every
INTERNAL stop or start — the post-suspend bounce, ensure's respawn and its
desired-state reconciliation, resume's own proxy start, the reboot sweep's
relaunch — only READS it. The writer and the clearer live here, and the
dispatcher in helm/seat.py is their one caller; the caller census in
tests/test_seat_down.py fails the day a second one appears.

AN UNREADABLE RECORD FAILS TOWARD SUPERVISION, AND SAYS SO. `read` answers
three ways: absent (supervise), a valid record (leave it down), or an error.
The error is deliberately treated like ABSENT by every supervisor, and every
surface prints it. The two wrong directions do not cost the same: supervising
a seat an operator wanted down costs one proxy process and a visible row the
operator fixes with one `seat down`; honouring a record helm cannot read
could keep a seat the fleet depends on dead with no statement of intent
behind it, which is the silent-starvation class the watchdog exists for. A
stale or corrupt marker must never be the thing that silently keeps a seat
dead, so it never keeps one dead at all.
"""
import json
import os

from . import home, pk, seat

MARKER = "desired-down.json"
VERSION = 1
UNKNOWN = "UNKNOWN"
REASON_CAP = 200


def path(family, seat_name):
    """Where this seat's desired-down record lives: its own proxy home."""
    return os.path.join(seat._proxy_home(family, seat_name), MARKER)


def _writer():
    """The DECLARED seat name of this process, or UNKNOWN. A hostile name is
    no name: the seam raises rather than hand back control bytes."""
    try:
        return home.chat_name() or UNKNOWN
    except home.SeatNameError:
        return UNKNOWN


def _launder(text, cap):
    """One line of display-safe text: no control or format characters (they
    could reshape the terminal or the chat post this rides in), bounded."""
    from .seats_common import _clip, _scrub
    return _clip(_scrub(str(text)).replace("\t", " ").strip(), cap)


def _invalid(rec, family, seat_name):
    """Why `rec` is not this seat's desired-down record, or None."""
    if not isinstance(rec, dict):
        return "not a JSON object"
    if rec.get("v") != VERSION:
        return "version %r, want %d" % (rec.get("v"), VERSION)
    if rec.get("seat") != seat_name or rec.get("family") != family:
        return "names seat %r of family %r, not %s of %s" % (
            rec.get("seat"), rec.get("family"), seat_name, family)
    for key in ("by", "at"):
        if not isinstance(rec.get(key), str) or not rec[key].strip():
            return "field %r missing or not text" % key
    if rec.get("reason") is not None and not isinstance(rec["reason"], str):
        return "field 'reason' is not text"
    return None


def _unique(pairs):
    """A JSON object whose field is named twice is not one statement."""
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON field %s" % key)
        out[key] = value
    return out


def read(family, seat_name):
    """(record, error) — (None, None) when the seat carries no record, (rec,
    None) for a valid one, (None, why) when a record is present and helm
    cannot read it. The third answer SUPERVISES; see the module docstring.

    Read through `pk.open_regular` (a FIFO planted at the path must never
    hang a supervisor) and parsed here, strictly: ABSENT is only a path that
    does not exist, and a dangling symlink is present-and-unreadable."""
    where = path(family, seat_name)
    try:
        with pk.open_regular(where, encoding="utf-8") as f:
            rec = json.load(f, object_pairs_hook=_unique)
    except FileNotFoundError:
        if not os.path.islink(where):
            return None, None
        return None, "%s is a dangling symlink" % where
    except Exception as exc:                # noqa: BLE001 — every failure is a reason, never a verdict
        return None, "%s unreadable (%s: %s)" % (
            where, exc.__class__.__name__, exc)
    why = _invalid(rec, family, seat_name)
    if why:
        return None, "%s is not a desired-down record (%s)" % (where, why)
    return rec, None


def read_seat(seat_name):
    """`read` for a bare seat name, resolving its family the way every seat
    verb does. A seat with no proxy family (a native or orca-adopted seat) has
    no proxy home and therefore no record: (None, None)."""
    family, err = seat._seat_family(seat_name)
    if err or not family or family == seat.NATIVE_FAMILY:
        return None, None
    return read(family, seat_name)


def mark(family, seat_name, reason=None):
    """(record, error) — record that the operator wants this seat DOWN.

    Written whole or not at all: a reader never sees half a record, and a
    failed write leaves any prior record exactly as it was."""
    rec = {"v": VERSION, "seat": seat_name, "family": family,
           "by": _writer(), "at": pk.now_ts(),
           "reason": _launder(reason, REASON_CAP) if reason else None}
    try:
        pk.atomic_write(path(family, seat_name),
                        json.dumps(rec, sort_keys=True) + "\n", mode=0o600)
    except OSError as exc:
        return None, "%s could not be written (%s)" % (
            path(family, seat_name), exc)
    return rec, None


def unmark(family, seat_name):
    """(was, error) — remove the record. `was` is the record it removed, the
    text of the read error for a record that could not be read, or None when
    there was nothing to remove. Never creates a directory."""
    rec, why = read(family, seat_name)
    try:
        os.unlink(path(family, seat_name))
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, "%s could not be removed (%s)" % (
            path(family, seat_name), exc)
    return rec if rec is not None else why, None


def describe(rec):
    """`desired-down by X at T (reason: R)` — the one rendering every surface
    prints, so `doctor --ensure`, `seat status`, the reboot sweep and
    proxywatch all say the same words about the same record."""
    reason = rec.get("reason")
    return "desired-down by %s at %s (%s)" % (
        _launder(rec.get("by"), 64), _launder(rec.get("at"), 32),
        "reason: " + _launder(reason, REASON_CAP) if reason
        else "no reason given")


def resume_hint(seat_name):
    return "`helm seat up %s` resumes supervision" % seat_name


def unreadable_text(seat_name, why):
    """The loud sentence for a record helm cannot read — and what it did."""
    return ("desired-down marker UNREADABLE — %s; SUPERVISED as if absent, "
            "because an unreadable marker must never keep a seat dead. "
            "`helm seat down %s` rewrites it, `helm seat up %s` removes it"
            % (why, seat_name, seat_name))


# ---------------------------------------------------------------------------
# THE OPERATOR'S DOORS — the only callers of `mark` and `unmark`
# ---------------------------------------------------------------------------

def operator_down(family, seat_name, reason=None):
    """`helm seat down <seat> [--reason R]`: record desired-down, then stop.

    RECORD FIRST, AND REFUSE TO STOP WITHOUT IT. A stop with no record is the
    exact failure this module ends — the next `doctor --ensure` pass respawns
    it — so a record that cannot be written refuses the whole verb and leaves
    the proxy running, which is at least a state the operator can see. A
    record that was written stands even when the stop then fails: the desired
    state is what the operator said, and every surface reports the running
    proxy against it as a contradiction rather than quietly adopting it.

    Only a seat with a MINTED proxy (a config.yaml in its own proxy home) can
    be recorded: a typo'd instance would otherwise grow a directory, and a
    record nothing enumerates is a record nothing honours."""
    import sys
    if seat._require_seat(family) is None:
        return 1
    ownership = seat._seat_surface_error(family, seat_name)
    if ownership:
        print("helm seat: " + ownership, file=sys.stderr)
        return 1
    cfg = os.path.join(seat._proxy_home(family, seat_name), "config.yaml")
    if not os.path.exists(cfg):
        from .seat_proxy import _mint_hint
        print("helm seat: %s has no minted proxy (%s is absent), so there is "
              "nothing to stop and no record to keep — `%s` mints it"
              % (seat_name, cfg, _mint_hint(family, seat_name)),
              file=sys.stderr)
        return 1
    rec, err = mark(family, seat_name, reason)
    if err:
        print("helm seat: refusing to stop %s — its desired-down record %s. "
              "A stop with no record is undone by the next `helm seat doctor "
              "--ensure` pass, so the proxy was left running"
              % (seat_name, err), file=sys.stderr)
        return 1
    print("helm seat: %s %s — `doctor --ensure`, the reboot sweep and "
          "proxywatch leave it down; %s"
          % (seat_name, describe(rec), resume_hint(seat_name)))
    rc = seat._down(family, seat=seat_name)
    if rc:
        print("helm seat: %s stays desired-down, but its proxy did not stop "
              "(above); every supervisor now reports the running proxy as a "
              "contradiction — re-run `helm seat down %s`"
              % (seat_name, seat_name), file=sys.stderr)
    return rc


def operator_run(seat_name, verb):
    """The operator's `up`, `resume <seat>` or `spawn`: clear desired-down.

    None means "proceed with the verb". rc 1 means the record could not be
    removed and the verb must not run: starting a proxy under a record that
    still says down would hand every supervisor a contradiction the operator
    never asked for. A seat whose family does not resolve to a proxy family,
    or whose path fails the surface-ownership proof, is left to the verb
    itself — it has no record helm could have written, and the verb's own
    refusal is the precise one."""
    import sys
    family, _seat = seat._split_seat(seat_name)
    if family not in seat.FAMILIES or family == seat.NATIVE_FAMILY \
            or seat._seat_surface_error(family, seat_name):
        return None
    was, err = unmark(family, seat_name)
    if err:
        print("helm seat: refusing `seat %s %s` — its desired-down record %s; "
              "remove it by hand, then re-run" % (verb, seat_name, err),
              file=sys.stderr)
        return 1
    if isinstance(was, dict):
        print("helm seat: %s desired-down record cleared (was %s) — "
              "supervised again" % (seat_name, describe(was)))
    elif was:
        print("helm seat: %s unreadable desired-down record removed (%s)"
              % (seat_name, was))
    return None
