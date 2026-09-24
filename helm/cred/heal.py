"""helm.cred heal cluster: live-holder probes and the heal verbs (see
__init__)."""
import os
import time

from .. import cred as _cred
from .. import homes
from ._common import _display_path
from .account import account_of
from .snapshots import (_census_pair, _foreign_family, _lineage_accounts,
                        _lineage_homes, _lineage_record, _snapshot_expiry,
                        _snapshot_family, restore, snapshots_for_home_name)


def _login_cmd(path):
    shown = _display_path(path)
    return (homes.LOGIN_CMDS["claude"](shown) if shown != "<redacted-path>"
            else "select the home by name, then run claude /login (path redacted)")


# ------------------------------------------------------------- live holders ---
def _proc_start(pid_dir):
    """Starttime from /proc/<pid>/stat, whose comm may contain spaces/parens."""
    with open(os.path.join(pid_dir, "stat")) as fh:
        return int(fh.read().rsplit(")", 1)[1].split()[19])


def _proc_uid(pid_dir):
    """The uid /proc itself reports for this pid (seam for tests)."""
    return os.stat(pid_dir).st_uid


# Every comm a claude-harness process wears. Live census (this box,
# 2026-07-22): claude hosts show comm `claude`; the node processes a session
# spawns (MCP servers, workers) show `node` / `node-MainThread` (a
# worker-thread rename; /proc comm is 15 bytes) — and a claude launched
# without its argv0 rename would itself read `node`. DELIBERATELY conservative
# in the safe direction: membership means a ptrace-protected pid stays
# UNCERTAINTY, so listing too much only costs heal coverage, while listing too
# little is what could evict a live session. `claude`-prefixed comms are
# family wholesale (claude-code, claude-<anything>) for the same reason.
_CLAUDE_FAMILY_COMMS = frozenset({"claude", "node", "node-MainThread"})


def _comm_claude_family(comm):
    return comm in _CLAUDE_FAMILY_COMMS or comm.startswith("claude")


def _protected_not_holder(pdir):
    """POLICY for the ptrace-protected residue (systemd --user with CapPrm,
    non-dumpable ssh-agent, sandbox children, git helpers — ~239 same-uid pids
    on the live box): their environ is EACCES forever, but /proc/<pid>/comm is
    world-readable (0444) even for non-dumpable processes. A same-uid pid
    whose comm is readable and NOT claude-family is structurally not a holder
    — a claude session cannot wear `systemd`'s comm. A claude-family comm, or
    a comm that cannot be read, stays uncertainty: the caller returns None and
    every mutation refuses (fail closed exactly where a live session could be
    evicted)."""
    try:
        with open(os.path.join(pdir, "comm")) as fh:
            comm = fh.read().strip()
    except OSError:
        return False
    return not _comm_claude_family(comm)


def _unprovable_note():
    """cannot-probe, precisely: the platform has no /proc at all, or a
    SAME-UID process defeated the scan. The old single string blamed a
    missing /proc even on hosts where /proc was right there."""
    if not os.path.isdir(_cred.PROC_ROOT):
        return "no /proc on this platform — cannot prove the home is free"
    return ("a same-uid process could not be proven free (a probe read failed "
            "while the pid persisted, or a ptrace-protected pid wears a "
            "claude-family comm)")


def holders_of(path, default=False):
    """[(pid, comm)] every live process pinned to this config dir (our own pid
    excluded). None means uncertainty, and every caller MUST refuse.

    A holder of a claude config dir necessarily runs as OUR uid — it reads
    this home's 0600 credentials — so foreign-uid pids are structurally not
    holders, and their kernel-unreadable environs never poison the scan
    (before this scoping, ANY multi-user host made every scan return None).
    Each same-uid pid is bracketed by its starttime so PID reuse cannot mix
    one process's environ with another's comm. A process that vanishes
    mid-scan is absence; a SAME-UID read error while the pid remains is
    uncertainty — UNLESS the pid's world-readable comm proves it outside the
    claude family (_protected_not_holder), which is the only thing that keeps
    heal alive on a real desktop where systemd --user, ssh-agent and sandbox
    children hold EACCES environs forever."""
    if not os.path.isdir(_cred.PROC_ROOT):
        return None
    real = os.path.realpath(path)
    me, uid = os.getpid(), os.geteuid()
    out = []
    try:
        entries = list(os.scandir(_cred.PROC_ROOT))
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        pid, pdir = int(entry.name), entry.path
        try:
            if _cred._proc_uid(pdir) != uid:
                continue
            start = _cred._proc_start(pdir)
            try:
                with open(os.path.join(pdir, "environ"), "rb") as fh:
                    env = fh.read()
            except OSError:
                if not os.path.exists(pdir):
                    continue                    # vanished mid-scan: absence
                if _protected_not_holder(pdir):
                    continue     # ptrace-protected, comm proves non-claude
                return None
            with open(os.path.join(pdir, "comm")) as fh:
                comm = fh.read().strip()
            if _cred._proc_start(pdir) != start or _cred._proc_uid(pdir) != uid:
                return None
        except (OSError, ValueError, IndexError):
            if not os.path.exists(pdir):
                continue
            return None
        val = None
        for var in env.split(b"\0"):
            if var.startswith(b"CLAUDE_CONFIG_DIR="):
                val = os.fsdecode(var.split(b"=", 1)[1])
                break
        if val is None:
            if default and comm == "claude":
                out.append((pid, comm))
            continue
        if os.path.realpath(os.path.expanduser(val)) == real:
            out.append((pid, comm))
    return sorted(out)


def _held_note(holders, cap=3):
    """The refusal message: AGENT processes first (the session that actually
    owns the home), then whatever inherited the env, capped — the full roster
    stays on the plan for --json."""
    ranked = sorted(holders, key=lambda h: (h[1] not in ("claude", "codex"), h[0]))
    head = ", ".join("pid %d" % p for p, _ in ranked[:cap])
    extra = len(ranked) - cap
    return head + (" +%d more process%s holding this dir"
                   % (extra, "es"[:2 * (extra != 1)]) if extra > 0 else "")


# -------------------------------------------------------------------- heal ---
def _family_elsewhere(snapshot, target, estate):
    """(home, live) — the home other than the target whose credentials carry
    this snapshot's token family NOW (live=True), or EVER per the recorded
    lineage (live=False); (None, False) when neither. helm's oldest
    credential law: one home = one token family; a byte-copy across two homes
    is the revocation bomb (homes.py's shared-family audit). Restoring a
    snapshot another home still holds would MINT that state — and restoring
    one a borrower ever REFRESHED (rotating, i.e. consuming, the snapshot's
    copy) trips server-side reuse detection, which revokes the whole family
    and bricks the live borrower. The live-bytes compare goes blind one
    borrower rotation later; the lineage does not.

    KNOWN CORNER (eviction-clean over-refusal, documented not yet closed):
    when heal ITSELF evicts a family from a foreign home — snapshotting the
    occupant as a pre-image, then overwriting with the named account — that
    family is no longer live anywhere and was never REFRESHED (heal does not
    run the credential, so nothing consumed the snapshot's copy). Yet the
    lineage still records it "ever live" in that foreign home, so a later heal
    of the evicted account's OWN home from its own snapshot trips the
    live=False lineage clash and is refused as a revocation-risk. The refusal
    is conservative-safe (a fresh login always recovers), merely stricter than
    necessary for this one clean-eviction shape. Closing it needs an
    evicted-clean marker distinguishing a heal-performed eviction from a
    borrower rotation, or a fall-back to the newest non-refused snapshot;
    deliberately deferred here to avoid weakening the revocation-bomb refusal
    on the highest-stakes path."""
    fam = snapshot.get("family") or _snapshot_family(snapshot["path"])
    if not fam:
        return None, False
    for other in estate:
        if other["real"] != target["real"] and other.get("family") == fam:
            return _display_path(other["name"]), True
    mine = ({target["name"], os.path.basename(target["real"])}
            | set(target.get("aliases") or []))
    seen = sorted(h for h in _lineage_homes(fam) if h not in mine)
    if seen:
        return _display_path(seen[0]), False
    return None, False


def _pair_misbound(snapshot, target):
    """The identity-discontinuity EVIDENCE that a snapshot filed under one
    account carries ANOTHER account's token bytes — the real mid-/login tear
    — or None. A tear is an identity discontinuity, never a timing skew: the
    routine same-account refresh-rotation also rewrites the token file
    moments before the Stop-hook capture while the identity file sits
    legitimately older, and THAT snapshot is the normal pre-image (the
    freshly rotated token is exactly what the guard exists to keep); flagging
    it torn would refuse the only valid recovery. Token bytes carry no
    identity of their own, so the discontinuity is judged on family claims:
    the snapshot's family under another account's snapshots (the completing
    /login files them at the next turn boundary), under another account in
    the lineage census, or live in the target home under the very occupant
    heal would evict. A tear that left none of those traces (torn capture
    chased by a second /login before any turn boundary) stays invisible —
    one refusal layer among several, backed by _capture_home's stat brackets
    (recorded in meta for forensics) and the post-restore identity+family
    verify. The opposite tear (fresh identity over stale tokens) is caught
    at capture by _foreign_family and the lineage-account check, which also
    covers the first-ever capture of a home."""
    fam = snapshot.get("family") or _snapshot_family(snapshot["path"])
    if not fam:
        return None
    other = _foreign_family(fam, snapshot["account"])
    if other:
        return "its token family is claimed by %s's snapshots" % other
    foreign = sorted(_lineage_accounts(fam) - {snapshot["account"]})
    if foreign:
        return ("the lineage records its token family live under %s"
                % foreign[0])
    if (target.get("family") == fam and target["account"]
            and target["account"] != snapshot["account"]):
        return ("its token bytes are the ones the current occupant (%s) "
                "holds live" % target["account"])
    return None


def _home_snapshot(snaps, target, estate):
    """A foreign home's newer backup must not mask this home's safe pre-image.

    Only the newest snapshot captured HERE is a candidate, never an arbitrary
    older token: a later capture here may have superseded or consumed it. The
    account-wide newest remains the refusal witness if that candidate cannot
    satisfy all unattended safety checks. No backup or lineage is discarded.
    """
    latest = snaps[-1] if snaps else None
    if latest is None or not _family_elsewhere(latest, target, estate)[0]:
        return latest
    own = next((s for s in reversed(snaps)
                if s.get("source_home") == target["real"]), None)
    if own is None or own is latest:
        return latest
    exp = _snapshot_expiry(own["path"])
    if (exp is None or exp < time.time() * 1000
            or not (own.get("family") or _snapshot_family(own["path"]))
            or _family_elsewhere(own, target, estate)[0]
            or _pair_misbound(own, target)):
        return latest
    return own


def heal_plan(name=None, record=False):
    """One plan per DRIFTED home: what heal WOULD do, and why it can't.
    status: ready | held | cannot-probe | no-backup | ambiguous-backup |
    revocation-risk (apply adds: restored | failed | no-preimage, and the
    hook path adds stale-preimage | torn-pair | expiry-unknown). record=True
    (the apply path only — dry-runs stay filesystem no-ops) also persists the
    family-lineage census of the current estate (family, home, account), so
    a later plan can refuse a snapshot whose family was EVER live in another
    home even after the borrower rotates."""
    plans = []
    estate = _cred.rows()
    if record:
        # Re-read each home's (family, account) under a stat bracket rather than
        # trusting the estate row, whose family (from homes_list) and account
        # (from a later verdict_for) were read at DIFFERENT times: a /login
        # landing between them would file the evicted family under the arriving
        # account. A home whose two files move mid-read is dropped this cycle
        # (the next stable census records it) — never recorded as a torn pair.
        seen = []
        for r in estate:
            pair = _census_pair(r["real"])
            if pair is None:
                continue
            fam, email = pair
            seen.append((fam, os.path.basename(r["real"]), email))
        _lineage_record(seen)
    for r in estate:
        if r["verdict"] != "DRIFT":
            continue
        if name and name not in ([r["name"], os.path.basename(r["real"])]
                                 + list(r["aliases"] or [])):
            continue
        want = os.path.basename(r["real"])       # the NAME's promise (folded email)
        snaps = [s for s in snapshots_for_home_name(want) if s["has_creds"]]
        snap_accounts = {s["account"] for s in snaps if s["account"]}
        holders = _cred.holders_of(r["real"], default=r["default"])
        snap = _home_snapshot(snaps, r, estate)
        clash, clash_live = (_family_elsewhere(snap, r, estate)
                             if snap else (None, False))
        plan = {"name": _display_path(r["name"]), "path": r["real"], "holds": r["account"],
                "wants_account_folded": want,
                "restore_from": snap["path"] if snap else None,
                "restore_account": snap["account"] if snap else None,
                "holders": holders or []}
        if holders is None:
            plan["status"], plan["reason"] = "cannot-probe", (
                _unprovable_note() + " — refusing to touch it")
        elif holders:
            plan["status"], plan["reason"] = "held", (
                "held by %s — a live session is never evicted" % _held_note(holders))
        elif not snaps:
            plan["status"], plan["reason"] = "no-backup", (
                "no snapshot for %s — the evicted account can only come back "
                "through a fresh login" % _display_path(want))
        elif len(snap_accounts) != 1:
            plan["status"], plan["reason"] = "ambiguous-backup", (
                "snapshots under %s claim multiple or missing account identities — "
                "the folded directory name is not enough to choose safely" % want)
        elif clash and clash_live:
            plan["status"], plan["reason"] = "revocation-risk", (
                "that snapshot's token family is LIVE in %s — restoring it here "
                "would leave byte-copies of ONE refresh token in two homes, and "
                "reuse detection revokes the whole family. Fresh login instead: %s"
                % (clash, _login_cmd(r["real"])))
        elif clash:
            plan["status"], plan["reason"] = "revocation-risk", (
                "that snapshot's token family was seen LIVE in %s — whatever "
                "refreshed it there ROTATED (consumed) the snapshot's copy, and "
                "a consumed refresh token is what reuse detection revokes a "
                "whole family over. A family ever live in another home never "
                "restores. Fresh login instead: %s"
                % (clash, _login_cmd(r["real"])))
        else:
            # STALENESS is the temporal twin of the shared-family bomb: an
            # access token that had already expired means whoever held this
            # home next had to refresh, and the grant ROTATES the refresh
            # token — the snapshot's copy may already be consumed, and a
            # consumed refresh token is what reuse detection revokes families
            # over. A TORN pair (identity-discontinuity evidence that the
            # snapshot binds one account's tokens to another's identity) is
            # its spatial twin. Neither is a manual refusal (this is still
            # the only recovery on disk — the owner sees the warning before
            # typing --apply), but the hook path REFUSES all three: stale,
            # torn, and an expiry it cannot even read.
            exp = _snapshot_expiry(snap["path"])
            plan["stale_pre_image"] = bool(exp is not None
                                           and exp < time.time() * 1000)
            plan["expiry_unknown"] = exp is None
            plan["torn_pair"] = _pair_misbound(snap, r)
            plan["status"], plan["reason"] = "ready", (
                "restore %s from %s" % (snap["account"] or want, snap["ts"]))
            if plan["torn_pair"]:
                plan["reason"] += (
                    " — WARNING: %s, so this snapshot may bind one account's "
                    "tokens to another account's identity (a backup that fired "
                    "mid-/login); restoring it can misroute a credential. A "
                    "fresh login is the safe move: %s"
                    % (plan["torn_pair"], _login_cmd(r["real"])))
            if plan["stale_pre_image"]:
                plan["reason"] += (
                    " — WARNING: that snapshot's access token was already expired, "
                    "so the home very likely refreshed (and ROTATED the refresh "
                    "token) after it was taken; the snapshot's copy may be spent, "
                    "and a spent refresh token is what reuse detection revokes a "
                    "family over. A fresh login is the safe move: %s"
                    % _login_cmd(r["real"]))
            if plan["expiry_unknown"]:
                plan["reason"] += (
                    " — note: the snapshot carries no readable access-token "
                    "expiry, so its freshness cannot be proven; the unattended "
                    "guard refuses what it cannot prove")
        plans.append(plan)
    return plans


def heal(name=None, apply=False, hook=False):
    """DRY-RUN BY DEFAULT. With apply=True, for every `ready` plan: re-probe
    holders (the window between plan and act is where a race lives), snapshot
    the CURRENT occupant so the undo is undoable, restore, then VERIFY the
    home now reads as the expected account AND its token family is live
    nowhere else — rolling back if either fails. hook=True is the unattended
    guard path (--quiet): a plan the manual path would only WARN about
    (torn_pair, stale_pre_image, expiry_unknown) is REFUSED outright,
    because the hook auto-types --apply and swallows the warning no owner
    will ever read."""
    plans = heal_plan(name, record=apply)
    if not apply:
        return {"apply": False, "plans": plans}
    for plan in plans:
        if plan["status"] != "ready":
            continue
        if hook and plan.get("torn_pair"):
            plan["status"], plan["reason"] = "torn-pair", (
                "%s — a backup that fires mid-/login can bind one account's "
                "tokens to another account's identity, and restoring the pair "
                "would misroute a credential. Auto-restore refused; `helm "
                "cred heal --apply` by hand accepts the risk knowingly, or "
                "log in fresh: %s"
                % (plan["torn_pair"], _login_cmd(plan["path"])))
            continue
        if hook and plan.get("stale_pre_image"):
            plan["status"], plan["reason"] = "stale-preimage", (
                "snapshot predates a token refresh (its access token had "
                "already expired), so its refresh token may be consumed — and "
                "a consumed refresh token is what reuse detection revokes a "
                "whole family over. Auto-restore refused; `helm cred heal "
                "--apply` by hand accepts the risk knowingly, or log in "
                "fresh: %s" % _login_cmd(plan["path"]))
            continue
        if hook and plan.get("expiry_unknown"):
            plan["status"], plan["reason"] = "expiry-unknown", (
                "the snapshot carries no readable access-token expiry, so its "
                "freshness cannot be proven — and the unattended path never "
                "restores what it cannot prove (an unparseable expiry must "
                "fail closed, not open). `helm cred heal --apply` by hand "
                "accepts the risk knowingly, or log in fresh: %s"
                % _login_cmd(plan["path"]))
            continue
        holders = _cred.holders_of(plan["path"])
        if holders is None or holders:
            plan["status"] = "held" if holders else "cannot-probe"
            plan["reason"] = ("held by %s (arrived mid-heal) — refused"
                              % _held_note(holders)) if holders else \
                             _unprovable_note() + " — refusing"
            continue
        pre = _cred.backup(plan["path"], apply=True)    # the evicted-now occupant, first
        if not pre["ok"]:
            # THE LAW, ENFORCED not merely attempted: no eviction without a
            # pre-image. Proceeding here would delete the occupant's only copy
            # of a live credential — the exact loss heal exists to prevent.
            plan["status"], plan["reason"] = "no-preimage", (
                "cannot snapshot the current occupant %s first (%s) — refusing "
                "to evict an account whose credentials would then exist nowhere"
                % (plan["holds"] or "?", pre["reason"]))
            continue
        plan["pre_image"] = pre.get("dest")
        holders = _cred.holders_of(plan["path"])
        if holders is None or holders:
            plan["status"] = "held" if holders else "cannot-probe"
            plan["reason"] = ("held by %s (arrived during pre-image capture) — refused"
                              % _held_note(holders)) if holders else \
                             _unprovable_note() + " — refusing"
            continue
        res = restore(plan["restore_from"], plan["path"], require_free=True)
        if not res["ok"]:
            plan["status"], plan["reason"] = "failed", res["error"]
            continue
        got = account_of(plan["path"])
        problem = None
        if not (got["ok"] and homes.canonical_name(got["email"])
                == plan["wants_account_folded"]):
            problem = "post-restore identity is %s" % (got["email"] or got["error"])
        else:
            # Post-restore family cross-check: a borrower that came alive
            # between the plan's census and the commit would leave byte-copies
            # of one refresh token in two homes — the exact state heal refuses
            # to mint. Verify what is checkable, roll back what is not clean.
            fam = _snapshot_family(plan["restore_from"])
            alive = ([o for o in _cred.rows() if o["real"] != plan["path"]
                      and o.get("family") == fam] if fam else [])
            if alive:
                problem = ("the restored token family is LIVE in %s (arrived "
                           "mid-heal) — byte-copies of one refresh token in "
                           "two homes" % _display_path(alive[0]["name"]))
        if problem is None:
            plan["status"] = "restored"
            plan["reason"] = ("%s is home again (creds may be stale — refresh "
                              "tokens rotate; if claude rejects them, log in "
                              "fresh into this home)" % got["email"])
            continue
        plan["status"], plan["reason"] = "failed", problem + " — rolled back"
        if pre.get("dest"):
            rolled = restore(pre["dest"], plan["path"], require_free=True)
            if not rolled["ok"]:
                plan["reason"] += "; pre-image rollback refused or failed"
    return {"apply": True, "plans": plans}
