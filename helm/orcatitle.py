"""The pane TITLE as an owner-facing seat checksum — written, never read.

THE OWNER'S PROBLEM. Orca generates every pane's tab title from pane content,
and the owner uses those titles to tell at a glance which agent is in which
tab. They are human-editable, so he corrects them by hand; a restored workspace
can replace a hand-set title with an auto-generated summary.
A title he cannot trust is a title he has to re-derive, one pane at a time,
from scrollback.

WHAT A CORRECT TITLE SAYS: THE SEAT NAME, EXACTLY AS THE FLEET BOARD SHOWS IT,
AND NOTHING ELSE. The title's job is to be matched against the board by eye —
read a tab strip, read the board, see the same strings. Anything appended
breaks that match, so there is no worktree, no state, no decoration and no
separator. `_seat_label` is what the board renders and therefore what goes on
the tab: it leaves a legitimate name BYTE-IDENTICAL and strips the ESC/bidi a
hostile `HELM_CHAT_NAME` could carry into the operator's terminal.

THE INVARIANT THAT GOVERNS EVERY LINE BELOW: A TITLE IS AN OUTPUT, NEVER AN
AUTHORITY. Nothing in helm may read a pane title to decide who a seat is — a
title is copyable, mutable and settable by anyone with the pane, which is
exactly why `seat_lifecycle_runtime._resolve_registered_pane` derives identity
from the process. This module writes titles and reads exactly one thing back
from them: whether the string already equals the one it would write. That read
answers "is this write necessary", never "whose pane is this", and the arms in
tests/test_orcatitle.py pin the difference by driving a pane whose title names
a DIFFERENT seat and proving the answer does not move.

A CONSEQUENCE THE TREE MUST KNOW, because the title text is now byte-equal to
a seat name by design: `_reap_stale` REFUSES to reap when an UNREGISTERED pane
carries a title byte-equal to a seat name, on the premise that such a title is
an identity claim helm did not make. That premise stops being true for panes
helm titled. The guard still fails CLOSED — it withholds a kill, it never
performs one — so the effect is more refusals, never a wrong reap, and the
refusal it produces is arguably the more honest answer for a pane helm can no
longer place. It is pinned by an arm rather than worked around here, because
the premise lives in that guard and only its owner can re-rule it.

WHOSE PANES GET WRITTEN. Only panes helm can NAME. On the fleet door that is
`orcaadopt.pane_rows`, the one place helm decides a pane's seat, which runs
entirely on process evidence and answers None for anything ambiguous,
contradictory or unclaimed; on the per-seat doors it is the caller's own
stronger proof. Everything else keeps the title orca generated for it: a WRONG
helm-written title is worse than an honest orca-written one.

THE HANDLE IS ALWAYS resolve_pane(pane_key)'s. Every door here takes its
handle from a row whose handle came from that resolution — the rebind proof,
`orcaadopt.resolve`, or `pane_rows` — never from a cached register field, a
title, or an inventory label.

FAILURE IS NEVER FATAL. The sweep that calls this exists to bring a fleet back
after a reboot; a tab label must never be able to fail that. Every write is
bracketed and degrades to a reported row.

WRITE HISTORY IS NOT CURRENT HOST PROOF. `_write` checks the rename reply's
echo, but the inventory's `title` is not a read of the tab's `customTitle`:
renderer-backed panes can keep reporting their generated summary after a
rename. Comparing only that field makes unattended sweeps repeat writes.
The ledger remembers successful assertions so those sweeps can converge.

An assertion is keyed by handle and PTY `incarnation_id`, with the desired
string. A changed incarnation or desired name defeats it; a missing incarnation
cannot justify suppression. But an incarnation is NOT a tab-title generation:
a host can restore or retitle a tab while its PTY survives. No guarantee that
every restart remints incarnations is made here, and helm does not depend on
orca's private persisted profile to fill that observational gap.

THE EXPLICIT REPAIR DOES NOT TRUST HISTORY. `seat retitle` plans a reassertion
for every namable pane, even if its inventory or ledger already matches. Its
dry run previews that same action without creating a lock or writing state.
Only unattended callers use the inventory/history shortcut. Thus an operator
can repair a same-incarnation clobber without restarting anything.

WRITERS SHARE ONE ORDER. The stable ledger lock covers reading history,
planning, invalidating attempted handles, host writes and recording replies.
Otherwise a delayed writer can record an assertion after a newer host write
and suppress its repair forever. Old assertions are removed BEFORE host writes,
so failure to record a reply costs a retry, not stale authority. Failure to
acquire the lock or persist invalidation withholds host mutation and reports
FAILED rows; it never falls back to an uncoordinated mutation.
"""
import json as _json
import os
import sys as _sys

from . import home

from .seats_common import _flocked, _scrub, _seat_label

#: How much of the title being REPLACED the plan table shows, and the marker
#: that says a rendering was cut.
_NOW_MAX = 34
ELISION = ".."

#: How much of a refusal reason fits beside a row before it stops being a
#: glanceable table.
_REASON_MAX = 160

#: Orca's own activity decorations on an AUTO-generated title. Recorded here
#: as an observation about the host, NOT consulted by any code in this module:
#: idempotence compares against the exact string helm wrote, so a decorated
#: title simply differs and is rewritten.
_ACTIVITY_GLYPHS = ("✳", "◐")

OK = "ok"                      # automatic shortcut: inventory or write history
WROTE = "titled"               # renamed
WOULD = "would title"          # dry run
FAILED = "not titled"          # degraded; the reason rides along


class Stamp(object):
    """One pane's title outcome. `current` is what orca reported, `desired` is
    what helm would write; both are carried so the owner-facing table can show
    the before and after without a second inventory read."""

    __slots__ = ("seat", "handle", "current", "desired", "action", "reason")

    def __init__(self, seat, handle, current, desired, action, reason=""):
        self.seat, self.handle = seat, handle
        self.current, self.desired = current, desired
        self.action, self.reason = action, reason

    def line(self):
        """THE TABLE SHOWS WHAT IT WOULD REPLACE. A dry-run default only
        protects the owner if he can see the thing about to be overwritten,
        and some of these tabs carry titles HE typed — measured on the live
        fleet, two of fifteen namable panes had hand-written titles naming the
        work rather than an orca-generated summary. A plan that printed only
        the new string would read as pure gain.

        The current title is an EXTERNAL string arriving from the metaharness
        and heading for a terminal, so it is scrubbed and clipped here, at the
        emit site. The opaque handle rides `--json` instead of this column: it
        is 42 characters of UUID and it was pushing the two strings the reader
        is actually comparing off the screen."""
        now = _scrub(self.current).strip() or "(none)"
        if len(now) > _NOW_MAX:
            now = now[:_NOW_MAX - len(ELISION)] + ELISION
        return "%-18s %-12s %-34s -> %s%s" % (
            _seat_label(self.seat), self.action, now, self.desired,
            "  — " + self.reason if self.reason else "")


def desired_title(seat_name):
    """The exact string helm wants on this pane's tab: the seat name as the
    board renders it.

    ONE FUNCTION, ONE SOURCE. `_seat_label` is what every other seat-name
    surface in the tree emits, so "matches the board" is a property of using
    the same renderer rather than a claim someone has to keep true by hand. It
    is also the launder: a seat name is a ROSTER KEY and an unvalidated join
    seam, and a tab title is a display sink.
    """
    return _seat_label(seat_name)


def one_line(err):
    """A refusal reason, flattened to fit a table row.

    MEASURED against the live host: a rename on a stale handle answers a
    HarnessError whose text embeds orca's whole pretty-printed JSON reply,
    newlines included. Every consumer of a reason renders it INSIDE one line —
    this module's own table, the sweep's `Row.line()`, the rename report — so
    a raw message breaks the surface that exists to be read at a glance.
    Flattened here, at the one place a reason is built, rather than at each of
    the renderers.
    """
    return " ".join(str(err).split())[:_REASON_MAX]


def state_path():
    return os.path.join(home.helm_home(), home.GLOBAL, ".state",
                        "orcatitle.json")


def asserted():
    """{handle: (incarnation_id, title)} — successful write history, not readback.

    Read through `pk` with a default, so a missing, unreadable or corrupt
    ledger yields an EMPTY map rather than raising: losing the memory costs a
    redundant rename, and a tab label may never fail a fleet sweep.
    """
    try:
        from . import pk
        raw = pk.read_json(state_path(), {}) or {}
    except Exception:                      # noqa: BLE001 — fail-open law
        return {}
    out = {}
    for handle, entry in (raw.items() if isinstance(raw, dict) else ()):
        if isinstance(entry, dict) and entry.get("title"):
            out[handle] = (entry.get("incarnation"), entry["title"])
    return out


def invalidate(stamps):
    """Remove attempted handles BEFORE host mutation, under the ledger lock.

    Persist even when a permissive read returns empty: an unreadable old entry
    must not reappear after the host changes. A failed replacement propagates
    to `restamp`, which withholds ALL host writes in this plan.
    """
    handles = {s.handle for s in stamps if s.action != OK}
    if not handles:
        return
    from . import pk
    p = state_path()
    st = pk.read_json(p, {}) or {}
    if not isinstance(st, dict):
        st = {}
    for handle in handles:
        st.pop(handle, None)
    pk.write_json(p, st)


def remember(stamps, rows):
    """Record successful writes while the caller holds the stable ledger lock.

    NEVER RAISES: the host write already happened. `invalidate` first removed
    the attempted handles, so failure here leaves no old assertion that could
    suppress a needed repair. The next automatic sweep can retry.
    """
    wrote = [s for s in stamps if s.action == WROTE]
    if not wrote:
        return
    by_handle = {r.get("handle"): r.get("incarnation_id")
                 for r in (rows or []) if r.get("handle")}
    try:
        from . import pk
        p = state_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        st = pk.read_json(p, {}) or {}
        if not isinstance(st, dict):
            st = {}
        for stamp in wrote:
            st[stamp.handle] = {"incarnation": by_handle.get(stamp.handle),
                                "title": stamp.desired}
        pk.write_json(p, st)
    except Exception:                      # noqa: BLE001 — fail-open law
        return


def _already_asserted(row, want, ledger):
    """Whether history records this desired string on this PTY incarnation.

    This is an unattended-write shortcut, not proof of the current tab title.
    A row with no incarnation cannot use it. Explicit repairs bypass it.
    """
    incarnation = row.get("incarnation_id")
    if not incarnation:
        return False
    seen = (ledger or {}).get(row.get("handle"))
    return bool(seen) and seen == (incarnation, want)


def plan(rows, ledger=None, force=False):
    """[Stamp] — the desired title for every pane helm can NAME.

    `rows` are `orcaadopt.pane_rows` rows. A row whose `seat` is None is a pane
    helm's identity join refused or never claimed, and it is ABSENT from the
    plan: its orca-generated title stands. Pure — writes nothing, calls
    nothing, so the whole rendering is testable without a metaharness.
    """
    out = []
    for row in rows:
        seat, handle = row.get("seat"), row.get("handle")
        if not seat or not handle:
            continue
        current = row.get("title") or ""
        want = desired_title(seat)
        # An explicit repair must remain possible after a same-incarnation
        # clobber, even when the inventory string happens to match as well.
        done = not force and (current == want or
                              _already_asserted(row, want, ledger))
        out.append(Stamp(seat, handle, current, want, OK if done else WOULD))
    return out


def restamp(adapter, rows, apply=False, force=False):
    """[Stamp] — serialize history, planning, host writes and their assertions.

    Automatic callers converge; the explicit repair passes `force=True` for
    BOTH preview and apply. A preview makes no directories or lock files.
    Lock or invalidation failure withholds host mutation, never the sweep.
    """
    if not apply:
        return plan(rows, asserted(), force=force)
    try:
        p = state_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with _flocked(p + ".lock") as lock:
            if lock.f is None:
                raise OSError("pane title lock unavailable")
            stamps = plan(rows, asserted(), force=force)
            invalidate(stamps)
            for stamp in stamps:
                if stamp.action != OK:
                    _write(adapter, stamp)
            remember(stamps, rows)
            return stamps
    except Exception as e:                 # noqa: BLE001 — fail-open sweep
        stamps = plan(rows, force=True)
        for stamp in stamps:
            stamp.action = FAILED
            stamp.reason = one_line("pane title assertion unavailable: %s" % e)
        return stamps


def restamp_one(adapter, seat_name, pane_row, apply=False):
    """Stamp | None — ONE pane whose seat the CALLER has already proven.

    The fleet door is `restamp` over `orcaadopt.pane_rows`, which pays a /proc
    walk. THE REBOOT PATH MUST NOT: `seat rebind` and the `seat resume --all`
    timer run unattended, and the sweep's measured contract is that a healthy
    fleet costs no walk at all. Both already hold a stronger proof than the
    pane_rows join — the rebind proof (register plus a live session or name,
    plus a pty-matched inventory row) for a registered seat, and
    `orcaadopt.resolve` for an adopted one — and the pane inventory is already
    in hand, so this entry spends nothing.

    THE CALLER OWNS THE REFUSAL HERE. `restamp` inherits "never stamp a pane
    helm cannot name" from the join; this entry inherits it from the caller,
    which must pass a seat name it PROVED and never one it guessed from a
    title, a cwd, or an inventory label.
    """
    if not pane_row or not seat_name:
        return None
    stamps = restamp(adapter, [dict(pane_row, seat=seat_name)], apply=apply)
    return stamps[0] if stamps else None


def after_rename(seat_name, adapter=None):
    """"" or one clause — the renamed seat's tab, re-asserted. NEVER raises.

    A RENAME IS THE OTHER CLOBBER, and the worse one. A reboot leaves a title
    that is obviously orca's; a rename leaves a title that is still a perfect
    seat-name checksum and names the WRONG SEAT, so it keeps reading as
    authoritative while it lies. The correction has to ride the rename itself
    — nothing else runs until the next sweep.

    Resolution goes through `orcaadopt.resolve` under the NEW name: the live
    process still exports the OLD `HELM_CHAT_NAME`, so the env join misses and
    the roster SESSION join is what finds the pane. When it does not, the
    answer is an empty clause and the tab waits for the sweep — never a guess.
    """
    try:
        from . import harness, orcaadopt
        ad = adapter or harness.detect()
        if ad is None or getattr(ad, "name", None) != "orca":
            return ""
        info = orcaadopt.resolve(seat_name, adapter=ad) or {}
        handle = info.get("handle")
        if not handle:
            return ""
        row = next((r for r in ad.list() if r.get("handle") == handle), None)
        stamp = restamp_one(ad, seat_name, row, apply=True)
    except Exception as e:        # noqa: BLE001 — fail-open law: a tab label
        return "\n  pane title NOT re-asserted: " + one_line(e)
    return note(stamp, "\n  pane ")


def note(stamp, prefix="; "):
    """The one clause a per-seat surface appends about its title, or "" when
    the automatic plan skips a write — the quiet contract, at the one place
    that clause is built. `prefix` is the CALLER's separator and rides inside
    the empty-string decision, so no caller has to re-implement "say nothing"
    by testing the clause it just asked for."""
    if stamp is None or stamp.action == OK:
        return ""
    return prefix + "%s %r%s" % (stamp.action, stamp.desired,
                                 " — " + stamp.reason if stamp.reason else "")


def _write(adapter, stamp):
    """One rename, bracketed. A missing verb, a stale handle, a dead daemon and
    a metaharness that is not orca all land in the same place: the row says
    FAILED with the reason, and the caller's sweep continues."""
    rename = getattr(adapter, "rename", None)
    if rename is None:
        stamp.action = FAILED
        stamp.reason = ("this metaharness exposes no pane rename verb, so a "
                        "title cannot be asserted here")
        return
    try:
        echoed = rename(stamp.handle, stamp.desired)
    except Exception as e:            # noqa: BLE001 — fail-open law: a tab
        stamp.action = FAILED         # label may never fail a fleet sweep
        stamp.reason = one_line(e)
        return
    if echoed is not None and echoed != stamp.desired:
        # THE EFFECT, NOT THE ABSENCE OF A COMPLAINT. orca echoes the stored
        # title in its reply; a reply that came back OK carrying something
        # else is a write that did not do what it said.
        stamp.action = FAILED
        stamp.reason = one_line(
            "the metaharness stored %r, not the requested title" % (echoed,))
        return
    stamp.action = WROTE
    stamp.current = stamp.desired


_USAGE = "seat retitle [--apply] [--json]"

HEADER = "%-18s %-12s %-34s    %s" % ("SEAT", "ACTION", "TAB SAYS NOW",
                                      "WOULD SAY")


def cmd_retitle(rest):
    """seat retitle [--apply] — assert every namable pane's title, on demand.

    THE OWNER'S FORCE BUTTON. Both preview and apply bypass the automatic
    inventory/history shortcut: a host-side title change need not replace the
    PTY incarnation. The read-only default shows every proposed reassertion;
    applying writes every namable pane even when helm previously titled it.

    IT MINTS NO WAKE. The re-stamp rides the timer that already owns
    `seat resume --all`; this verb is the operator typing, and neither adds a
    daemon.

    IT IS TITLES ONLY. `seat rebind --all --apply` also repairs titles for the
    registered population, but it REWRITES REGISTERS to do it and reaches no
    adopted seat at all — and the adopted population is where the owner's own
    coordinating panes live. Nobody should have to run a register-rewriting
    fleet verb to fix a tab label.
    """
    from .cli import guard_tail
    rc = guard_tail("helm seat retitle", rest, flags=("--apply", "--json"),
                    usage=_USAGE)
    if rc is not None:
        return rc
    apply = "--apply" in rest
    from . import harness, orcaadopt
    ad = harness.detect()
    if ad is None or getattr(ad, "name", None) != "orca":
        print("helm seat retitle: no orca metaharness detected — a pane title "
              "is asserted through its host and there is none here",
              file=_sys.stderr)
        return 1
    procs, unreadable = orcaadopt.claude_processes()
    # THE ADAPTER IS THREADED, not re-detected. `pane_rows` detects its own
    # when given none, and then the handles this verb renames would come from
    # one object while the rename went to another — two objects that happen to
    # agree today is not the same as one that must.
    rows, why = orcaadopt.pane_rows(adapter=ad, procs=procs,
                                    unreadable=unreadable)
    if why:
        print("helm seat retitle: no pane inventory — " + why, file=_sys.stderr)
        return 1
    stamps = restamp(ad, rows, apply=apply, force=True)
    unnamed = sum(1 for r in rows if not r.get("seat"))
    # THE CALLER THAT SUPPLIES THE CENSUS OWNS THE BLINDNESS (pane_rows' own
    # law). Blindness can only SUBTRACT from the named set — an unreadable
    # process contributes no handle-to-seat edge — so it costs a title, never
    # a wrong one. Reported rather than refused, because leaving a pane with
    # its orca title is the safe outcome and refusing the whole sweep is not.
    blind = orcaadopt.cannot_look(
        unreadable, "which panes belong to which seat")
    if "--json" in rest:
        print(_json.dumps(
            {"apply": apply, "unnamed_panes": unnamed, "blind": blind,
             "stamps": [{"seat": s.seat, "handle": s.handle,
                         "current": s.current, "desired": s.desired,
                         "action": s.action, "reason": s.reason}
                        for s in stamps]}, indent=2, sort_keys=True))
        return 1 if any(s.action == FAILED for s in stamps) else 0
    print(HEADER)
    for stamp in sorted(stamps, key=lambda s: (s.seat or "~", s.handle or "")):
        print(stamp.line())
    counts = {}
    for stamp in stamps:
        counts[stamp.action] = counts.get(stamp.action, 0) + 1
    replaced = sum(1 for s in stamps
                   if s.action in (WROTE, WOULD) and s.current.strip())
    print("helm seat retitle: %s%s; %d pane(s) helm cannot name keep their "
          "orca title" % (
              ", ".join("%d %s" % (counts[k], k)
                        for k in (OK, WROTE, WOULD, FAILED) if counts.get(k))
              or "no namable panes",
              "" if apply else " — DRY RUN, nothing written", unnamed))
    if replaced:
        # THE DESTRUCTIVE HALF, COUNTED. Some of these tabs were titled by the
        # owner, and "titled 12" reads as pure gain while hiding that it
        # overwrote twelve strings somebody may have typed on purpose.
        print("helm seat retitle: %d of those %s a title the tab already "
              "carried — the TAB SAYS NOW column is what %s"
              % (replaced, "replaced" if apply else "would replace",
                 "went" if apply else "would go"))
    if blind:
        print("helm seat retitle: " + blind)
    return 1 if counts.get(FAILED) else 0
