#!/usr/bin/env python3
"""helm seats — the meld-half's agent-facing lane, collapsed onto the chat
room (design: prd/2026-07-20-meldhalf-design.md, hardened per the codex
adversarial round §11 there). Meld carried a separate tmpfs whisper channel
because it had no room; helm HAS the room, so every capability here is a
READ PATTERN over /dev/shm/helm-chat plus small RAM state files — zero new
transports, zero daemons, zero slot files.

The lane is called DELIVERY, never "whisper" (that word is taken twice:
inject's first-turn brief digest, and the v1 on-ledger attestation frames).

TRUST DOMAIN — SAY IT LOUDLY: seat names are DISPLAY LABELS. Everything in
this module is advisory coordination between cooperating same-uid processes
in a 0700 tmpfs dir — not a security boundary, and never claimed as one
(principal cryptography stays dregg's; do not rebuild it here). What the
bindings below DO defend against is the realistic failure: an agent — or a
prompt-injected one — impersonating the owner or another holder through
legit tooling. Hence: owner-rule delivery trusts only rows the server-side
owner rails stamped (origin web/tui); claim leases bind to {session, lease
nonce, fence}, never to a matching display string.

The legs:
  * join    — SessionStart hook: roster row (RAM presence, keyed on the seat's
              HELM_CHAT_NAME so a seat joins as its family name) + cursor
              INITIALIZED HERE (a message posted between session start and
              the first tool boundary must deliver — codex H5.5) + the seat's
              identity/protocol line as session context. That line DIRECTS the
              agent to arm its idle-wake beacon (a persistent Monitor on
              `helm chat wait --follow`) as a MANDATORY first action — the only
              thing that wakes an idle PTY agent (native-wake-only-agent-armed).
  * deliver — PostToolUse hook: the tool-boundary nudge. At most ONE row per
              boundary, 200-byte clip, control-char scrub, information-not-
              instruction label. AT-LEAST-ONCE, NEVER AT-MOST-ONCE (codex
              H7): the hook response is emitted in ONE unbuffered write and
              the cursor commits only AFTER — a kill in between produces a
              duplicate next boundary, which beats silence. Every fire
              touches the seat's own `.seen` file (presence for free); the
              unchanged-room fast path never rewrites shared state.
  * wait    — the beacon: block until a row addressed to the seat lands
              (Monitor arms it). --follow keeps the room open and streams EACH
              new matching row as one line (one line = one agent wake), never
              returning on a match. NOTE (codex M11): this is busy-turn parity
              plus an idle beacon the join context line makes MANDATORY to arm —
              nothing external can wake an idle PTY agent, so the self-armed
              Monitor is the only path.
  * claims  — advisory TTL lease with session+nonce+fence binding and
              monotonic expiry (the worktree-collision class).
  * stop-guard — Stop hook: the IDLE GATE (buildr/mc arbiter capability,
              helm-native). BLOCKS a stop while undelivered mentions/owner
              rows sit past the seat's cursor (once per pending-fingerprint —
              never an infinite loop) or while THIS session holds a live
              claim lease; WARNs (never blocks) on a clean stop to arm the
              beacon; silently runs `helm index cap --apply`. Fail-open
              total; HELM_STOP_GUARD=0 kills it.

Council (embargoed verdicts) is DEFERRED to 0.3 — the codex round showed a
correct embargo needs an expected-set freeze, a reveal state machine, salted
commitments and batch-row reveal; the 0.3 spec is recorded in the design
doc §11. No live consumer today, so: record, don't build.

Cursor law (codex H5): `<room>.cursor.<seat>[.<sid8>]` holds {dev, ino, off,
rid, active} — the room file's identity, the byte offset of the first
unprocessed row, the last processed row's stable id, and whether the seat
actually consumed room traffic (an EOF join baseline is not presence). The cursor is PER (seat,
session): two live sessions sharing one HELM_CHAT_NAME each hold their own
cursor, so an @mention FANS OUT to all of them instead of being race-consumed
by whichever boundary fires first (the live @mention-loss class). A caller
with no session (bare CLI) rides the seat-level cursor; a fresh session
cursor seeds from the seat-level one when it exists (upgrade continuity —
rows tracked before the split are not skipped). Fast path = one stat (same inode, size
== off ⇒ nothing new; a same-size REPLACEMENT changes the inode and is
caught). Inode change or shrink ⇒ rotation/replacement: reset to 0 and use
rid to suppress the retained overlap (duplicates acceptable, loss is not).
All cursor transitions serialize on `<room>.cursor.<seat>.lock`; rows are
selected/committed by byte offset from ONE fstat'd fd, never by line count.
Initialized at JOIN. Every hook-facing path is FAIL-OPEN TOTAL.

MULTI-ROOM (slice 5 — the owner's live helm-dogfood '@opus-integrator' post
woke nothing, 2026-07-21): the lane is not main-scoped. deliver_any (the
PostToolUse hook) and the wait --follow beacon consider EVERY live room —
the seat's private DM lane first, then primary, then newest-activity rooms,
ROOM_SCAN_CAP-bounded — with the same per (seat, room, session) cursor
mechanics per room. A TRACKED seat meeting a cursor-less room BACKFILLS from
offset 0 (a room born after its join is all post-join news — the mention
that created the channel must deliver); an untracked seat keeps the EOF
self-heal everywhere (pre-join backlog never floods). join baselines every
existing room; stop-guard and the roster report read pending across the same
bounded scan.

BEACON SCOPE (premise beacon-scope-mentions-plus-home-room-owner-posts-not-
all — the live bug: codex-2, homed to #main, never saw an @codex-2 mention
posted in #helm-dogfood because homing ALLOWLISTED the scan): the scan
covers every live room; deliverable() applies the scope per row —
  (a) a @seat mention (or a {dm} row naming the seat) surfaces from ANY room,
      always — a direct address is never filtered;
  (b) ANYTHING in the seat's HOME room (roster home_room) surfaces — the
      team channel is full-surface for its own team;
  (c) @all broadcasts surface in {home, main}; owner-rail posts NO LONGER
      auto-wake (owner steer 2026-07-21: mentions + home room are enough) —
      never fleet-wide across every side room;
  (d) a MUTED room (helm chat seat mute <room> — roster row "mute") stops
      (b)/(c) noise at this seat; (a) still surfaces (mute tunes noise,
      never direct address).

DM (premise exact-token-addressee-match): seats.dm() writes ONE row into the
recipient's private lane (chat.dm_room — the `dm-` reserved namespace, a
dm/ subdir file no room list ever shows). The recipient is the EXACT seat
token (a casefold roster snap only — never a substring, never a slug fold:
team.a and team-a are different lanes by key). Delivery/beacon/stop-guard
pick the lane up first in the room scan; nobody else ever scans it.
"""
import getpass
import glob
import json
import os
import re
import sys
import time
import unicodedata

from . import chat, home, pk

MAX_BYTES = 200          # the delivery clip — meld's whisper frame budget
PREVIEW_CHARS = 80       # roster panel preview
SEAT_BYTES = 80          # the seat label clip — a name is a glance, not a payload
FRESH_S, QUIET_S = 120, 900
DEFAULT_TTL = 900        # claims lease default
SCAN_CAP = 512 * 1024    # deliver never reads more than this per room
ROOM_SCAN_CAP = 16       # rooms per boundary/beacon pass — the multi-room bound
OWNER_RAILS = ("web", "tui")  # server-side owner surfaces stamp these origins
# the join banner's onboarding pointer — resolved against THIS checkout so a
# seat in any cwd can open it; tests pin banner ↔ file together (moving the
# guide without repointing this breaks the suite, not the fleet)
GUIDE_PATH = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "docs",
    "NEW_AGENT_GUIDE.md"))
_BROADCAST = re.compile(r"(?<![A-Za-z0-9._-])@(all|fleet|everyone)(?![A-Za-z0-9._-])", re.I)


# ---------------------------------------------------------------------------
# identity + addressing
# ---------------------------------------------------------------------------

_FAMILIES = ("fable", "opus", "sonnet", "haiku", "kimi", "glm", "gpt",
             "gemini", "deepseek", "qwen", "grok", "mistral", "llama")


def _family():
    """The ambient model family, best-effort: the model env first (seat
    launches export CLAUDE_CODE_SUBAGENT_MODEL), else the harness. Display
    material for the auto-name — never identity, never authorization."""
    model = (os.environ.get("CLAUDE_CODE_SUBAGENT_MODEL")
             or os.environ.get("ANTHROPIC_MODEL") or "").lower()
    for fam in _FAMILIES:
        if fam in model:
            return fam
    if model:
        tok = pk.slug(model).split("-")[0]
        if tok:
            return tok
    if os.environ.get("CODEX_SESSION_ID"):
        return "codex"
    if (os.environ.get("CLAUDE_CODE_SESSION_ID")
            or os.environ.get("CLAUDE_SESSION_ID")
            or os.environ.get("CLAUDECODE")):
        return "claude"
    return "agent"


def auto_name(session, cwd=None):
    """G-stable-names: a MEANINGFUL stable auto-name for an un-named join —
    <project>-<family> ('helm-fable'), deduped with -2/-3… when a DIFFERENT
    session already holds the name. Stable: callers reach here only when the
    roster has no row for this session, and the result is immediately
    roster-bound (join / deliver self-heal), so the same session keeps
    resolving to the same seat. Opaque agent-<sid8> hex (12/15 of the live
    roster before this) is the last-resort floor only."""
    sid = str(session)
    proj = os.path.basename((cwd or "").rstrip(os.sep))
    base = pk.slug("%s-%s" % (proj, _family())) if proj else _family()
    r = roster()

    def taken(name):
        row = r.get(name)
        return bool(row) and row.get("session") != sid \
            and sid not in (row.get("sessions") or [])

    if not taken(base):
        return base
    for i in range(2, 100):
        cand = "%s-%d" % (base, i)
        if not taken(cand):
            return cand
    return "agent-" + sid[:8]


def derive_seat(session=None, cwd=None):
    """$HELM_CHAT_NAME first (the launch seam sets it), else a MEANINGFUL
    stable auto-name for the session (auto_name — project+family, deduped),
    else chat.whoname's law: a bare agent never gets the operator's
    identity."""
    name = home.chat_name()   # THE validated seam (home.chat_name): a hostile
    if name:                  # HELM_CHAT_NAME is rejected, never becomes a seat
        return name
    if session:
        return auto_name(session, cwd)
    return chat.whoname()


def _git_root(cwd):
    """The canonical checkout root, including normal worktrees + submodules.
    Bare repos and odd non-worktree layouts are not project contexts."""
    try:
        import subprocess
        inside = subprocess.run(
            ["git", "-C", cwd or ".", "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=5)
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return None
        common = subprocess.run(
            ["git", "-C", cwd or ".", "rev-parse", "--path-format=absolute",
             "--git-common-dir"], capture_output=True, text=True, timeout=5)
        if common.returncode != 0:
            return None
        path = common.stdout.strip().rstrip(os.sep)
        if os.path.basename(path) == ".git":
            return os.path.dirname(path)
        bare = subprocess.run(
            ["git", "--git-dir", path, "rev-parse", "--is-bare-repository"],
            capture_output=True, text=True, timeout=5)
        if bare.returncode == 0 and bare.stdout.strip() == "true":
            # Every worktree attached to one bare common-dir shares this root.
            return path
        # A submodule's common dir is <super>/.git/modules/<name>; its own
        # top-level remains the identity root. Odd non-bare git-dir layouts do
        # too; bare repositories without a worktree were rejected above.
        top = subprocess.run(
            ["git", "-C", cwd or ".", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5)
        return top.stdout.strip() if top.returncode == 0 else None
    except Exception:
        return None


def _fingerprinted_project(label, root):
    """A readable label plus canonical-path fingerprint that survives pk.slug's
    60-char cap. Used for every unregistered root and only those registered names
    whose normalized room label collides with another distinct registered path."""
    import hashlib
    real = os.path.realpath(root).rstrip(os.sep)
    fingerprint = hashlib.blake2b(
        real.encode("utf-8"), digest_size=8).hexdigest()
    base = pk.slug(label)[:60 - len(fingerprint) - 1]
    return "%s-%s" % (base, fingerprint)


def _path_project(root):
    """Stable fallback identity for an unregistered checkout, independent of
    scanner state. Registry adoption remains the deliberate human-naming seam."""
    real = os.path.realpath(root).rstrip(os.sep)
    return _fingerprinted_project(os.path.basename(real), real)


def _git_project(cwd):
    """The canonical HELM project name for a cwd, worktree-agnostic and
    collision-safe. Registry identity wins; every unregistered checkout gets a
    deterministic path-fingerprinted fallback independent of scanner knowledge."""
    root = _git_root(cwd)
    if not root:
        return None
    try:
        from . import registry
        projects = registry.load().get("projects") or {}
        real = os.path.realpath(root)
        paths = {
            name: os.path.realpath(row.get("path"))
            for name, row in projects.items() if row.get("path")
        }
        for name, path in paths.items():
            if path != real:
                continue
            room = pk.slug(name)
            collision = any(
                other_path != real and pk.slug(other) == room
                for other, other_path in paths.items()
            )
            return _fingerprinted_project(name, real) if collision else name
        return _path_project(real)
    except Exception:
        return _path_project(root) or None


def derive_home_room(cwd):
    """The seat's DEFAULT project room. Explicit env/CLI rooms are resolved by
    resolve_homing before this fallback. Project-less seats stay un-homed
    (legacy all-room behavior); any identity that normalizes to reserved #main
    stays un-homed."""
    proj = _git_project(cwd)
    room = pk.slug(proj) if proj else None
    return None if not room or room == "main" else room


def safe_cwd():
    """os.getcwd() failing OPEN to None when the process cwd no longer exists
    (a pruned lane worktree is a ROUTINE lifecycle state here, not an error).
    Every homing call site must use this instead of a bare os.getcwd(): an
    eager getcwd in the chat/hook prologue crashed every default chat verb and
    all three delivery hooks for a deleted-cwd session, BEFORE any fail-open
    guard could catch it. resolve_homing/derive_home_room treat None as
    un-homed, so the session keeps working (in #main) instead of dying."""
    try:
        return os.getcwd()
    except OSError:
        return None


def resolve_homing(cli_room=None, cwd=None):
    """THE one home-room precedence — every writer resolves through here and
    write_roster is the one enforcement gate behind it. The bug-class this
    kills: multiple derivations of one truth scattered a live roster's homes
    across 'main' (a defaulted mirror write), '<project>' (a cwd derivation)
    and '<env room>' (the launch seam) for seats of the SAME team. Order:
      1. an explicit CLI/operator room (cli_room)          -> explicit
      2. HELM_CHAT_ROOM (the launch seam) — explicit unless the seam stamped
         HELM_CHAT_ROOM_SOURCE=derived                     -> explicit/derived
      3. the cwd's git-project room (derive_home_room)     -> derived
    Returns (room, source); (None, None) = un-homed. Paired law (enforced in
    write_roster): a derived value may NEVER overwrite an explicit/operator
    one, so a re-join/resume/mirror can never downgrade a deliberate home."""
    if cli_room:
        return cli_room, "explicit"
    env_room, env_source = home.env_pair("CHAT_ROOM", "CHAT_ROOM_SOURCE")
    if env_room:
        return env_room, ("derived" if env_source == "derived" else "explicit")
    room = derive_home_room(cwd)
    return room, ("derived" if room else None)


def owner_names():
    """Display names the owner rails post under. HELM_CHAT_OWNER_NAMES csv
    overrides; default = 'david' (the web surface's name) + the unix login."""
    raw = home.env("CHAT_OWNER_NAMES")
    if raw is not None:
        return {n.strip().lower() for n in raw.split(",") if n.strip()}
    names = {"david"}
    try:
        names.add(getpass.getuser().lower())
    except Exception:
        pass
    return names


def _mention_re(seat):
    return re.compile(r"(?<![A-Za-z0-9._-])@" + re.escape(seat)
                      + r"(?![A-Za-z0-9._-])", re.I)


def seat_scope(seat, r=None):
    """The seat's beacon tuning + roster admission, one roster read. Computed
    ONCE per scan pass and threaded down — the poll path stays ~one stat per
    quiet room. `tracked` cannot depend only on the primary cursor: the primary
    room may not have existed when an otherwise-joined seat entered."""
    row = ((r if r is not None else roster()).get(seat) or {}) if seat else {}
    return {"home": row.get("home_room"),
            "mute": {pk.slug(x) for x in row.get("mute") or []},
            "tracked": bool(row.get("joined") or row.get("session")
                            or row.get("sessions"))}


def deliverable(m, seat, room="main", scope=None):
    """Does this row reach `seat` at a tool boundary, given the ROOM it sits
    in? The beacon-scope law (premise beacon-scope-mentions-plus-home-room-
    owner-posts-not-all), top to bottom:
      * reactions, AMBIENT rows and the seat's own posts: never. An ambient
        row ({ambient}: the todo mirror's status line) is machine state a
        teammate PULLS — it renders everywhere and wakes nobody, including
        in a home room, where the rule below would otherwise hand every
        plain row to every seat on the team.
      * a {dm} row: the EXACT-token recipient only (casefold — never a
        substring, never a slug fold), whatever lane it sits in.
      * an @seat mention: ANY room, ALWAYS — checked before mute, because a
        direct address is never noise.
      * a muted room (the seat's roster "mute" list): nothing further.
      * the seat's HOME room (roster home_room): EVERY remaining row — the
        team channel is full-surface for its own team.
      * {home, main}: @all broadcasts only. Owner-rail posts (origin web/tui)
        do NOT wake here (owner steer 2026-07-21: mentions + home-room are
        enough — an owner post reaches a seat via an @mention or its own home
        room, never as a plain main broadcast). NOT fleet-wide: a side room's
        @all drafts nobody homed elsewhere.
      * anything else (foreign-room chatter, incl. non-mention owner posts
        outside home): never (noise law).
    scope=None computes seat_scope here — hot paths pass it precomputed."""
    text = m.get("text")
    if not text or m.get("react") or m.get("ambient"):
        return False
    frm = str(m.get("from") or "")
    if frm.casefold() == str(seat or "").casefold():
        # Own-post suppression casefolds like EVERY seat-identity match here
        # (roster keys, mentions, dm, rfrom): after a case-only rename
        # (kimi -> Kimi) the seat's pre-rename rows still carry the old
        # casing, and an exact-case check would let the seat wake on its own
        # reply — the precise identity transition the rfrom rule below
        # protects (codex + codex-2 xrev of c2f4856, 2026-07-21).
        return False
    if m.get("dm"):
        # exact-token recipient (casefold only), OR the row sits in the
        # seat's OWN lane — the lane is the routing truth, so a rename's
        # carried-over history (rows naming the old token) still delivers
        return (str(m["dm"]).casefold() == str(seat or "").casefold()
                or (bool(seat) and room == dm_lane(seat)))
    if _mention_re(seat).search(text):
        return True
    rf = str(m.get("rfrom") or "")
    if rf and rf.casefold() == str(seat or "").casefold():
        # A REPLY to this seat's row is a direct address of its author — the
        # same tier as an @mention, any room, before mute. This inverts the
        # original "threading is invisible to the beacon" law deliberately:
        # the owner's stated WHY for replies was "I'm tired of typing agent
        # names to mention" (2026-07-22) — replying INSTEAD OF mentioning is
        # the feature, so a reply that wakes nobody delivers the mechanism
        # while dropping its purpose. rfrom is the parent's recorded author,
        # stamped at post time; only the parent's author wakes, so a reply
        # stays quieter than the mention it replaces ever was. Casefold, like
        # every seat-identity match here (roster keys, mentions, dm): a
        # case-only rename must not silently drop direct reply delivery.
        return True
    sc = scope if scope is not None else seat_scope(seat)
    if room in sc["mute"]:
        return False
    home_r = sc.get("home")
    if home_r and room == home_r:
        return True
    if room != "main" and room != home_r:
        return False
    # @all broadcasts still wake in {home, main}. Owner-rail posts NO LONGER
    # auto-wake (owner steer 2026-07-21: mentions + home-room are enough — an
    # owner post reaches a seat only via an @mention or its own home room, never
    # as a plain main-room broadcast). OWNER_RAILS/owner_names stay for owner
    # IDENTITY (forgery defense) elsewhere; owner-posts are simply not a wake
    # class. bug-class superseded: beacon-owner-post-wake-is-noise.
    return bool(_BROADCAST.search(text))


def _scrub(s):
    """Meld's reader-side defense, ported: strip anything that could reshape
    the single-line label the content rides in — C0/C1 controls, format
    chars, line/paragraph separators. Tab survives."""
    return "".join(ch for ch in s if ch == "\t"
                   or unicodedata.category(ch) not in ("Cc", "Cf", "Zl", "Zp"))


def _clip(s, cap=MAX_BYTES):
    """Byte-budget clip on a codepoint boundary (meld's re-clip law)."""
    enc = s.encode("utf-8")
    if len(enc) <= cap:
        return s
    end = cap
    while end > 0 and (enc[end] & 0xC0) == 0x80:
        end -= 1
    return enc[:end].decode("utf-8", errors="ignore") + "…"


# ---------------------------------------------------------------------------
# small flock helper (premise.py's pattern; every shared-state RMW uses it)
# ---------------------------------------------------------------------------

class _flocked:
    """flock a STABLE sibling lock file (never a file that atomic-replace
    swaps out under the lock). Fail-open: no lock ⇒ proceed unlocked — the
    guarantee degrades, the operation never dies on the lock."""

    def __init__(self, path):
        self.path, self.f = path, None

    def __enter__(self):
        try:
            import fcntl
            self.f = open(self.path, "a")
            fcntl.flock(self.f.fileno(), fcntl.LOCK_EX)
        except OSError:
            self.f = None
        return self

    def __exit__(self, *exc):
        if self.f is not None:
            try:
                import fcntl
                fcntl.flock(self.f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            self.f.close()
        return False


# ---------------------------------------------------------------------------
# roster (RAM presence) — full row at join; per-seat `.seen` touch on the
# hot path (codex H8 / freeze bar 6: deliver never rewrites shared state)
# ---------------------------------------------------------------------------

def roster_path():
    return os.path.join(chat.chat_dir(), ".roster.json")


def _seat_key(seat):
    """The seat's STATE-FILE key: readable slug + a short hash of the
    casefolded raw seat. pk.slug alone collides ('api.a' and 'api-a' both
    slug to 'api-a'), and a shared cursor lets one seat silently CONSUME the
    other's rows (codex B1 — loss, not a duplicate). Every per-seat state
    path (cursor, cursor lock, seen) derives from this one key; case-only
    variants fold together deliberately — case-insensitive @mentions cannot
    address them apart anyway."""
    import hashlib
    h = hashlib.blake2b(str(seat).casefold().encode("utf-8"),
                        digest_size=4).hexdigest()
    return "%s-%s" % (pk.slug(seat), h)


def seen_path(seat):
    return os.path.join(chat.chat_dir(), ".seen." + _seat_key(seat))


def roster():
    return pk.read_json(roster_path(), {}) or {}


def touch_seen(seat):
    """The hot-path presence beat: utime a per-seat empty file — no shared
    read-modify-write, no lock, no lost sibling rows."""
    p = seen_path(seat)
    try:
        os.utime(p)
    except OSError:
        try:
            with open(p, "w"):
                pass
        except OSError:
            pass


def last_seen(seat, row=None):
    try:
        return os.stat(seen_path(seat)).st_mtime
    except OSError:
        return (row or {}).get("last_seen")


SESSIONS_KEPT = 8   # co-named sessions remembered per roster row (addressing)


def write_roster(seat, session=None, cwd=None, home_room=None,
                 home_room_source=None):
    """The one-time (join) roster write — keyed by seat. `session` is the
    newest writer; every co-named session is ALSO kept in row["sessions"]
    (newest last, capped) so seat_for_session resolves ALL of them and each
    keeps its own delivery cursor (fan-out, never race-consume). home_room_source
    is `explicit` or `derived`: explicit joins may deliberately move a seat;
    derived joins follow a seat across projects only while its prior home was
    also derived; they never undo an explicit/operator home or clear. An
    UNLABELED home_room reads as derived — unknown provenance takes the
    weakest tier, never the strongest (the old back-compat seam stamped it
    explicit and let a spawn mirror downgrade a deliberate home)."""
    chat._ensure_dir()
    with _flocked(roster_path() + ".lock"):
        r = roster()
        row = r.get(seat) or {}
        home_room = pk.slug(home_room) if home_room else None
        if home_room_source == "explicit":
            # Known tier gap (documented; follow-up card): explicit beats
            # explicit regardless of AGE, so an operator rehome holds only
            # until a pane launched with env HELM_CHAT_ROOM (explicit, no
            # derived stamp) restarts — its SessionStart join re-writes the
            # stale env room. The homing law only forbids DERIVED downgrades;
            # ranking 'operator' above a stale explicit env (or re-minting
            # launch.sh on rehome) is the candidate fix, deliberately not
            # smuggled into this lane.
            old = row.get("home_room")
            if home_room != old:
                newly_admitted = _rooms_to_baseline(old, home_room)
                _baseline_rooms(seat, row, newly_admitted)
                if old and home_room == "main" and not newly_admitted:
                    _backfill_missing_room_cursors(
                        "main", seat, row.get("sessions") or [])
            if home_room:
                row["home_room"] = home_room
            else:
                row.pop("home_room", None)
            row["home_room_source"] = "explicit"
        elif home_room:
            # derived — or UNLABELED (provenance unknown reads as derived, the
            # weakest tier): fills a never-homed row or follows a derived-tier
            # one — and an EXISTING home with no source IS derived-tier, so it
            # follows too (a pre-upgrade row {home: main, source: None} must
            # not freeze its stale scattered value against every later derived
            # join). It can never overwrite an explicit/operator home or
            # clear, so a re-join/resume/mirror never downgrades a deliberate
            # home.
            old, source = row.get("home_room"), row.get("home_room_source")
            if source in (None, "derived") and home_room != old:
                _baseline_rooms(
                    seat, row, _rooms_to_baseline(old, home_room))
                row["home_room"] = home_room
            if row.get("home_room") == home_room \
                    and source in (None, "derived"):
                row["home_room_source"] = "derived"
        if session:
            row["session"] = str(session)
            sess = [s for s in row.get("sessions") or [] if s != str(session)]
            sess.append(str(session))
            row["sessions"] = sess[-SESSIONS_KEPT:]
        if cwd:
            row["cwd"] = cwd
            row["project"] = os.path.basename(cwd.rstrip(os.sep)) or cwd
        if not row.get("joined"):
            row["joined"] = pk.now_ts()
        row["last_seen"] = time.time()
        r[seat] = row
        pk.write_json(roster_path(), r)
    touch_seen(seat)
    return row


def seat_for_session(session):
    if not session:
        return None
    sid = str(session)
    for seat, row in roster().items():
        if row.get("session") == sid or sid in (row.get("sessions") or []):
            return seat
    return None


def _resolve_seat(r, token):
    """A roster key, else the seat whose session (or 8+-char prefix of one)
    matches — how the owner names a live agent they only know by sid."""
    if token in r:
        return token
    t = str(token or "")
    if len(t) >= 8:
        for seat, row in r.items():
            sess = [row.get("session") or ""] + list(row.get("sessions") or [])
            if any(s == t or s.startswith(t) for s in sess if s):
                return seat
    return None


_STATE_MARKERS = (".cursor.", ".seen.", ".stopfp.", ".scan.")


def _key_bounded(name, key):
    """Does this state filename belong to THIS seat key? Match only at a
    FIELD BOUNDARY: after '<marker><key>' the name must end or continue with
    '.' (the .k<sid8>/.lock suffixes — _seat_key's slug+hash alphabet never
    contains '.'). A bare substring test cross-fired: seat 'foo' (key
    foo-<h1>) prefix-matched every state file of a seat literally NAMED
    'foo-<h1>' (its key foo-<h1>-<h2>), so pruning/renaming 'foo' unlinked or
    moved the LIVE seat's cursors — the same gc state cross-fire class the
    case-variant fix closed, substring flavor (fable adversarial probe B3)."""
    for m in _STATE_MARKERS:
        probe, i = m + key, 0
        while True:
            i = name.find(probe, i)
            if i < 0:
                break
            end = i + len(probe)
            if end == len(name) or name[end] == ".":
                return True
            i += 1
    return False


def _bounded_sub(name, ok, nk):
    """_key_bounded's boundary law applied to the RENAME substitution: swap
    the key only where it fills a whole '.'-field (bare, or dm-prefixed for
    the dm-lane room segment) — keys/rooms never contain '.' (slug + hash
    alphabets), so '.' is a hard field boundary. The raw str.replace it
    replaces rewrote a ROOM slug that merely EMBEDS the key
    ('<key>-updates.cursor.<key>' -> room segment corrupted), silently
    detaching the cursor from its room (fable adversarial probe C10)."""
    dm_ok, dm_nk = chat.DM_PREFIX + ok, chat.DM_PREFIX + nk
    return ".".join(nk if s == ok else (dm_nk if s == dm_ok else s)
                    for s in name.split("."))


def _move_seat_state(old, new):
    """Carry every state file from the old seat key to the new one — cursors
    (+ per-session variants + locks), .seen, stop latches, every room. The
    tracked delivery ground survives a rename; an EOF re-baseline would be
    silent loss. Fail-open per file."""
    ok, nk = _seat_key(old), _seat_key(new)
    d = chat.chat_dir()
    try:
        os.replace(chat.room_path(chat.DM_PREFIX + ok),   # the private DM lane
                   chat.room_path(chat.DM_PREFIX + nk))   # rides the rename too
    except OSError:
        pass
    try:
        names = os.listdir(d)
    except OSError:
        return
    for n in names:
        if _key_bounded(n, ok):
            try:
                # replace EVERY key occurrence: a dm-lane cursor carries the
                # key twice (dm-<key>.cursor.<key>…) and both must move —
                # the lane file kept its inode, so the cursor stays valid.
                # Segment-bounded (_bounded_sub), never raw str.replace: a
                # room slug embedding the key must keep its room segment.
                os.replace(os.path.join(d, n),
                           os.path.join(d, _bounded_sub(n, ok, nk)))
            except OSError:
                pass


def rename_seat(old, new):
    """(ok, message). G-stable-names: bind a live agent to a memorable @name.
    `old` is a roster seat name or a session id (full, or an 8+-char prefix).
    Rebinds delivery: the roster row moves (so the hook's session_id resolves
    to the new name) and every keyed state file moves with it. The seat's
    HELM_CHAT_NAME env (if it launched with one) still names the OLD seat —
    the message says so; a beacon armed on the old name must be re-armed."""
    new = (new or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", new):
        return False, ("new name %r must be 1-64 chars of [A-Za-z0-9._-] "
                       "(what an @mention can address)" % new)
    if new.lower() in owner_names() or _BROADCAST.search("@" + new):
        return False, "%r is reserved (an owner/broadcast name)" % new
    with _flocked(roster_path() + ".lock"):
        r = roster()
        seat = _resolve_seat(r, old)
        if seat is None:
            return False, ("no roster row matches %r (a seat name or an "
                           "8+-char session prefix — helm chat seats --all)" % old)
        if seat == new:
            return True, "seat is already named %s" % new
        # case-INSENSITIVE taken-check: _seat_key casefolds, the reserved check
        # lowers, and _mention_re is re.I — a case-variant name (KIMI vs kimi)
        # is the SAME address + the SAME keyed state downstream, so two such
        # rows alias mentions, share presence, and cross-fire gc's state
        # unlink onto the live seat (kimi cross-family review, live-probed
        # 2026-07-21).
        # Exclude `seat` itself so a pure self-case-change isn't falsely blocked.
        if any(k != seat and k.casefold() == new.casefold() for k in r):
            return False, ("seat name %r is taken (case-insensitive — the "
                           "roster keys casefold; helm chat seats --all)" % new)
        r[new] = r.pop(seat)
        pk.write_json(roster_path(), r)
        _move_seat_state(seat, new)
    old_lbl = _seat_label(seat)   # raw key drove r[new]=r.pop(seat); echoed
    return True, ("seat %s -> %s: @%s now delivers to it. If it armed a "  # old
                  "beacon on the old name, re-arm: Monitor(command: \"helm "  # name
                  "chat wait --seat %s --follow\", persistent: true) — if Monitor "
                  "is not in your surface it is DEFERRED: ToolSearch(query: "
                  "\"select:Monitor\") first. A seat "  # laundered (new is validated
                  "launched with HELM_CHAT_NAME=%s re-registers the old name "  # safe)
                  "on its next session — relaunch to make the rename stick "
                  "there." % (old_lbl, new, new, new, old_lbl))


def set_mute(seat, room, on=True):
    """(ok, message) — the seat's own beacon filter (the beacon-scope
    premise's tuning control). A muted room stops surfacing home-room
    chatter / @all at this seat; a direct @seat mention or a DM
    ALWAYS still surfaces — mute tunes noise, never direct address. Stored
    on the roster row so every lane (boundary, beacon, stop-guard, report)
    reads one truth."""
    room = pk.slug(room)
    with _flocked(roster_path() + ".lock"):
        r = roster()
        row = r.get(seat) or {}
        mute = [m for m in row.get("mute") or [] if m != room]
        if on:
            mute.append(room)
        row["mute"] = sorted(mute)
        r[seat] = row
        pk.write_json(roster_path(), r)
    lbl = _seat_label(seat)   # raw key drove the dict write above; the echoed
    if on:                    # label is laundered (a hostile HELM_CHAT_NAME
        return True, ("%s muted for %s — @%s mentions and DMs still surface "
                      "(unmute: helm chat seat unmute %s)"  # must not reshape
                      % (room, lbl, lbl, room))             # the CLI terminal)
    return True, "%s unmuted for %s" % (room, lbl)


def mutes(seat):
    """The seat's muted rooms, sorted — one roster read."""
    return sorted((roster().get(seat) or {}).get("mute") or [])


def _allowed_rooms(home_room):
    """Current live rooms admitted by one home choice (used only to compute a
    rehome delta; the delivery chokepoint remains _scan_rooms)."""
    try:
        rooms = set(chat.list_rooms()) | {"main"}
    except OSError:
        rooms = {"main"}
    return {home_room, "main"} if home_room else rooms


def room_in_scope(room, row):
    """Whether `room` belongs to a roster row's CURRENT delivery scope.
    Un-homed rows retain all-room legacy scope; a home admits only itself + main.
    Cursor activity is historical, so web presence must intersect it with this."""
    home_room = (row or {}).get("home_room")
    return not home_room or pk.slug(room) in {home_room, "main"}


def _rooms_to_baseline(old, new):
    """Rooms whose pre-transition history must be skipped. A destination home
    is always re-baselined, even when the old un-homed scope could theoretically
    see it; clearing to all rooms baselines only newly admitted foreign rooms."""
    if new and new != old:
        # A homed seat already admits #main. Narrowing that scope to explicit
        # main must preserve pending main traffic; every other destination is
        # newly admitted (including legacy un-homed -> homed, which rebases a
        # potentially stale all-room cursor by design).
        return set() if old and new == "main" else {new}
    return _allowed_rooms(new) - _allowed_rooms(old)


def _baseline_rooms(seat, row, rooms):
    """Baseline newly admitted rooms at their current EOF for the seat and all
    remembered sessions. Rehome changes scope, never replays pre-admission
    history or traffic accumulated while the seat was away."""
    sessions = [s for s in ([row.get("session")] + list(row.get("sessions") or []))
                if s]
    for room in rooms:
        _baseline_room_cursors(room, seat, sessions)


def rehome_seat(token, room):
    """(ok, message). The DELIBERATE home-room move (multi-project isolation):
    set a seat's roster home_room — the operator's explicit re-home (the only
    path that changes an EXISTING seat's home unless a later join carries its
    own explicit room). `room` is slugged; 'main'/'none'/'-' clears the home
    (back to all-rooms un-homed). Newly admitted rooms baseline at current EOF,
    so pre-rehome history never wakes, delivers, or stop-gates the seat."""
    room = (room or "").strip().lower()
    clear = room in ("", "main", "none", "-", "all")
    home_room = None if clear else pk.slug(room)
    with _flocked(roster_path() + ".lock"):
        r = roster()
        seat = _resolve_seat(r, token)
        if seat is None:
            return False, ("no roster row matches %r (a seat name or an "
                           "8+-char session prefix — helm chat seats --all)"
                           % token)
        row = r.get(seat) or {}
        old = row.get("home_room")
        # raw seat key drove _resolve_seat + the dict write; the echoed seat
        # label AND the roster-borne old home_room are laundered so neither a
        # hostile HELM_CHAT_NAME nor a planted home_room reshapes the terminal.
        lbl, old_lbl = _seat_label(seat), _seat_label(old) if old else old
        if clear and not old and row.get("home_room_source") == "operator":
            return True, "seat %s is already un-homed (all rooms)" % lbl
        if not clear and home_room == old \
                and row.get("home_room_source") == "operator":
            return True, "seat %s is already homed to #%s" % (lbl, home_room)
        new_home = None if clear else home_room
        newly_admitted = _rooms_to_baseline(old, new_home)
        _baseline_rooms(seat, row, newly_admitted)
        if clear:
            row.pop("home_room", None)
        else:
            row["home_room"] = home_room
        row["home_room_source"] = "operator"
        r[seat] = row
        pk.write_json(roster_path(), r)
        if clear:
            return True, ("seat %s re-homed %s -> un-homed (all rooms); "
                          "takes effect on its next delivery scan"
                          % (lbl, old_lbl or "un-homed"))
    return True, ("seat %s re-homed %s -> #%s; delivery is now { #%s, #main } "
                  "— takes effect on its next delivery scan (no relaunch)"
                  % (lbl, old_lbl or "un-homed", home_room, home_room))


# ---------------------------------------------------------------------------
# the cursor (codex H5) + the tail scan both deliver and the report use
# ---------------------------------------------------------------------------

def _sid8(session):
    """The session's cursor-key token — filename-safe, 8 chars, None-safe."""
    return pk.slug(str(session))[:8] if session else None


def cursor_path(room, seat, session=None):
    """PER (seat, session) when a session is known — co-named sessions each
    keep their own cursor (fan-out; the @mention-loss fix). Sessionless
    callers (bare CLI) ride the seat-level file."""
    p = os.path.join(chat.chat_dir(),
                     "%s.cursor.%s" % (pk.slug(room), _seat_key(seat)))
    s8 = _sid8(session)
    return "%s.%s" % (p, s8) if s8 else p


def _cursor(room, seat, session=None):
    d = pk.read_json(cursor_path(room, seat, session), None)
    if isinstance(d, dict) and isinstance(d.get("off"), int):
        return d
    return None


def _write_cursor(room, seat, dev, ino, off, rid, session=None, active=False,
                  skip=None):
    chat._ensure_dir()
    row = {"dev": dev, "ino": ino, "off": off, "rid": rid,
           "active": bool(active)}
    if skip:
        row["skip"] = skip
    pk.write_json(cursor_path(room, seat, session), row)


def _baseline_state(room, at_start=False):
    """Room identity + offset + final complete row id for a safe baseline.
    Keeping the row id lets inode replacement suppress already-baselined rows."""
    try:
        with open(chat.room_path(room), "rb") as f:
            st = os.fstat(f.fileno())
            if at_start or not st.st_size:
                return st.st_dev, st.st_ino, 0 if at_start else st.st_size, None
            start = max(0, st.st_size - SCAN_CAP)
            f.seek(start)
            data = f.read()
        chunks = data.split(b"\n")
        if data and not data.endswith(b"\n"):
            chunks.pop()
        if start and chunks:
            chunks.pop(0)
        rid = None
        for chunk in reversed(chunks):
            if not chunk:
                continue
            try:
                row = json.loads(chunk.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("id"):
                rid = row["id"]
                break
        return st.st_dev, st.st_ino, st.st_size, rid
    except OSError:
        return None, None, 0, None


def _write_cursor_path(path, state, active=False):
    dev, ino, off, rid = state
    pk.write_json(path, {"dev": dev, "ino": ino, "off": off, "rid": rid,
                         "active": bool(active)})


def _cursor_paths(room, seat, sessions=()):
    """Seat-level plus every known/on-disk session cursor for one room."""
    base = cursor_path(room, seat)
    paths = {base}
    paths.update(cursor_path(room, seat, s) for s in sessions)
    paths.update(p for p in glob.glob(base + ".*") if not p.endswith(".lock"))
    return paths


def room_active(room, seat):
    """Whether any cursor for this seat actually consumed traffic in `room`.
    Normal hook delivery advances a session cursor, not the seat baseline, so
    presence must aggregate every on-disk cursor just like rehome safety does."""
    for path in _cursor_paths(room, seat):
        cur = pk.read_json(path, None)
        if isinstance(cur, dict) and cur.get("active"):
            return True
    return False


def _backfill_missing_room_cursors(room, seat, sessions=()):
    """Start missing cursors at zero when a room was already logically admitted
    but did not exist at the seat's join. Seat-level first, so session cursors
    inherit the same post-join ground instead of EOF-dropping pending traffic."""
    for session in [None] + list(sessions):
        if _cursor(room, seat, session) is not None:
            continue
        path = cursor_path(room, seat, session)
        with _flocked(path + ".lock"):
            if _cursor(room, seat, session) is None:
                _init_cursor(room, seat, session, at_start=True)


def _baseline_room_cursors(room, seat, sessions=()):
    """Baseline seat-level, remembered, and every on-disk session cursor.
    The roster keeps only eight session ids for addressing; cursor safety may
    not inherit that cap because an older still-live session can return later."""
    state = _baseline_state(room)
    for path in _cursor_paths(room, seat, sessions):
        with _flocked(path + ".lock"):
            _write_cursor_path(path, state)


def _init_cursor(room, seat, session=None, at_start=False):
    """Baseline at the CURRENT end of room — at JOIN time (codex H5.5), so
    everything posted after session start delivers at the first boundary.
    A fresh SESSION cursor inherits the seat-level baseline when one exists
    (pre-split installs tracked the seat file; those rows must not be
    skipped by an EOF re-baseline — loss is the one forbidden outcome).
    at_start=True baselines at OFFSET 0 instead (multi-room: a room born
    after the seat joined is all post-join news — the mention that created
    the channel must deliver, not vanish under an EOF baseline), still
    binding the room file's identity so rotation detection holds.
    `active` distinguishes a bare EOF join from actual room consumption.
    -> True iff the baseline was inherited (already-tracked ground)."""
    if session:
        base = _cursor(room, seat)
        if base:
            _write_cursor(room, seat, base.get("dev"), base.get("ino"),
                          base["off"], base.get("rid"), session=session,
                          active=base.get("active"), skip=base.get("skip"))
            return True
    state = _baseline_state(room, at_start=at_start)
    _write_cursor(room, seat, *state, session=session)
    return False


def _tail(room, cur):
    """Read one bounded window of complete rows from ONE fstat'd fd.
    -> (dev, ino, base_off, entries, ground_rid, skip_rid), or None when
    there is nothing to read. entries = [(row_dict|None, end_off)] for every
    COMPLETE line; a trailing partial line is never consumed.

    Rotation/replacement restarts at zero and suppresses everything through
    the cursor's last row id. That id can sit beyond the first SCAN_CAP window
    in a legitimately rotated room, so `skip` persists the suppression search
    across boundaries: old rows are parked, never emitted. If a complete pass
    reaches EOF without the id, the replacement did not retain it; reset once
    to zero and accept duplicate replay on the next boundary (loss is the one
    forbidden outcome)."""
    path = chat.room_path(room)
    try:
        f = open(path, "rb")
    except OSError:
        return None
    with f:
        st = os.fstat(f.fileno())
        off, suppress = cur.get("off", 0), cur.get("skip")
        if (st.st_dev, st.st_ino) != (cur.get("dev"), cur.get("ino")) \
                or st.st_size < off:
            off, suppress = 0, cur.get("rid")   # rotation / replacement
        elif st.st_size == off:
            if suppress:
                return st.st_dev, st.st_ino, 0, [], None, None
            return None                          # genuinely nothing new
        f.seek(off)
        data = f.read(SCAN_CAP)
    entries, pos = [], off
    for chunk in data.split(b"\n"):
        end = pos + len(chunk) + 1
        if end > off + len(data):                # trailing partial line
            break
        row = None
        try:
            v = json.loads(chunk.decode("utf-8", errors="replace"))
            if isinstance(v, dict):
                row = v
        except ValueError:
            pass
        if chunk:
            entries.append((row, end))
        pos = end
    base, ground = off, cur.get("rid")
    if suppress is not None:
        for i, (row, end) in enumerate(entries):
            if row is not None and row.get("id") == suppress:
                base, entries, ground = end, entries[i + 1:], suppress
                suppress = None
                break
        if suppress is not None:
            complete_eof = off + len(data) == st.st_size \
                and (not data or data.endswith(b"\n"))
            if complete_eof:
                if off == 0:
                    return st.st_dev, st.st_ino, 0, entries, None, None
                return st.st_dev, st.st_ino, 0, [], None, None
            base = entries[-1][1] if entries else off
            return st.st_dev, st.st_ino, base, [], ground, suppress
    return st.st_dev, st.st_ino, base, entries, ground, None


def dm_lane(seat):
    """The seat's own private DM lane, as a reserved-namespace room name."""
    return chat.DM_PREFIX + _seat_key(seat)


def scan_path(seat, session=None, lane="deliver"):
    """Per-seat/session/lane overflow-ring state for eventual room coverage."""
    p = os.path.join(chat.chat_dir(), ".scan." + _seat_key(seat))
    s8 = _sid8(session)
    return ".".join(x for x in (p, s8, lane) if x)


def _fair_room_slice(names, seat, session, size, lane):
    """A bounded round-robin over stable room identities. Survivors retain
    their queue order while newly discovered rooms append behind them, so
    insertions cannot move an old room's goalpost forever. Each consumer lane
    owns its queue: roster observation must never advance delivery or stop."""
    live = set(names)
    if not live or size <= 0:
        return []
    path = scan_path(seat, session, lane)
    try:
        with _flocked(path + ".lock"):
            state = pk.read_json(path, {}) or {}
            saved = state.get("rooms")
            saved = saved if isinstance(saved, list) else []
            ring = []
            seen = set()
            for name in saved:
                if isinstance(name, str) and name in live and name not in seen:
                    ring.append(name)
                    seen.add(name)
            ring.extend(sorted(live - seen))
            count = min(size, len(ring))
            out = ring[:count]
            pk.write_json(path, {"rooms": ring[count:] + out})
            return out
    except OSError:
        return sorted(live)[:size]


def _scan_rooms(primary="main", seat=None, scope=None, session=None,
                scan_lane=None, bounded=True):
    """Rooms considered by one delivery/gate pass. The seat's private DM lane,
    primary room, home room, and main are pinned first. Foreign rooms use a
    ROOM_SCAN_CAP-bounded overflow budget on hot paths; when that budget
    overflows, a persisted identity queue per consumer lane gives every room
    eventual coverage without observation stealing delivery slots. Join is
    the one-time `bounded=False` caller so every existing room receives an EOF
    admission baseline and pre-join backlog can never emerge in a later slice.

    The scan is deliberately scope-BLIND (premise beacon-scope-mentions-
    plus-home-room-owner-posts-not-all superseded the G1-G3 homing allowlist):
    an @mention anywhere must eventually surface, while deliverable() applies
    per-row scope. DM lanes other than the seat's own stay invisible because
    list_rooms() omits them. Fail-open: an unlistable dir is just the pins."""
    rooms = []
    if seat:
        lane = dm_lane(seat)
        try:
            if os.path.exists(chat.room_path(lane)):
                rooms.append(lane)
        except OSError:
            pass
    rooms.append(primary)
    if seat:
        sc = scope if scope is not None else seat_scope(seat)
        pins = [sc.get("home"), "main"]
    else:
        pins = ["main"]
    seen = {pk.slug(r) for r in rooms}
    for p in pins:
        if p and p not in seen and os.path.exists(chat.room_path(p)):
            rooms.append(p)
            seen.add(p)
    try:
        names = chat.list_rooms()
    except OSError:
        names = []
    others = [n for n in names if n not in seen]
    if not bounded:
        return rooms + sorted(others)
    size = ROOM_SCAN_CAP - 1
    if scan_lane and seat and len(others) > size:
        others = _fair_room_slice(
            others, seat, session, size, scan_lane)
    else:
        def mtime(n):
            try:
                return os.stat(chat.room_path(n)).st_mtime
            except OSError:
                return 0.0
        others.sort(key=mtime, reverse=True)
        others = others[:size]
    return rooms + others


def _room_dirty(room, seat, session=None):
    """Lock-free precheck: could `room` hold rows past the (seat, session)
    cursor? A missing cursor is dirty (a room this seat has never looked
    at). Otherwise ONE stat against the atomically-written cursor: same
    file identity and size == off ⇒ clean. False positives are fine
    (deliver re-checks under the lock); a false negative cannot happen —
    an append grows the size, a rotation/replacement changes the inode.
    This keeps the every-tool-call hot path at ~one stat per quiet room."""
    cur = _cursor(room, seat, session) or _cursor(room, seat)
    if cur is None:
        return True
    try:
        st = os.stat(chat.room_path(room))
    except OSError:
        return False                        # no room file: nothing to deliver
    return (st.st_dev, st.st_ino) != (cur.get("dev"), cur.get("ino")) \
        or st.st_size != cur.get("off")


def deliver(session=None, room="main", seat=None, emit=None, cwd=None,
            backfill=False, scope=None):
    """The tool-boundary nudge, ONE room: at most ONE deliverable row, oldest
    first; later matches stay PENDING (their count shows, their cursor ground
    is not consumed — codex H6). Returns the label line or None.

    At-least-once (codex H7): when `emit` is given it is called with the
    line BEFORE the cursor commits; emit must do its one unbuffered write.
    A kill between emit and commit re-delivers next boundary.

    Fan-out: the cursor is per (seat, session) — every co-named session sees
    the same @mention on its own boundary; consuming here never starves a
    sibling session (at-most-once BETWEEN co-named sessions was the bug).

    backfill=True (deliver_any's tracked-seat path) makes a MISSING cursor
    baseline at offset 0 and scan THIS boundary — the multi-room law for a
    room born after the seat joined; default keeps the EOF self-heal."""
    known = seat_for_session(session)
    seat = seat or known or derive_seat(session, cwd)
    touch_seen(seat)                       # presence FIRST — a seat muted by the
    if (home.env("CHAT_DELIVER") or "").lower() in ("0", "off", "no"):
        return None                        # kill-switch below is still ALIVE:
                                           # its presence beat keeps gc off it
    sc = scope if scope is not None else seat_scope(seat)
    # Global order is roster -> cursor: rehome/join baseline cursors while the
    # roster lock is held. Register an unknown session BEFORE taking its cursor
    # lock, otherwise delivery and rehome can each wait forever on the other.
    if session and known is None and _cursor(room, seat, session) is None:
        write_roster(seat, session=session)
    with _flocked(cursor_path(room, seat, session) + ".lock"):
        cur = _cursor(room, seat, session)
        if cur is None:
            inherited = _init_cursor(room, seat, session, at_start=backfill)
            if not inherited and not backfill:
                return None       # fresh EOF baseline: backlog never floods
            cur = _cursor(room, seat, session)  # already-tracked ground —
            if cur is None:                     # deliver from it THIS boundary
                return None
        got = _tail(room, cur)
        if got is None:
            return None
        dev, ino, base, entries, last_rid, skip = got
        hit = None
        last_end = base
        for i, (row, end) in enumerate(entries):
            if row is not None and deliverable(row, seat, room, sc):
                hit = (i, row, end)
                break
            last_end, last_rid = end, (row or {}).get("id") or last_rid
        if hit is None:
            _write_cursor(
                room, seat, dev, ino, last_end, last_rid, session=session,
                active=cur.get("active") or bool(entries), skip=skip)
            return None
        i, row, end = hit
        waiting = sum(1 for r, _e in entries[i + 1:]
                      if r is not None and deliverable(r, seat, room, sc))
        is_dm = room.startswith(chat.DM_PREFIX)           # a DM is a DM on
        where = (" dm" if is_dm                           # every surface —
                 else "" if room == "main" else " #%s" % room)  # never a room
        line = "[helm chat%s → %s] %s: %s" % (            # the reply must land
            where, seat, row.get("from") or "?",          # where the word came
            _clip(_scrub(row.get("text") or "")))
        if waiting:
            line += " (+%d waiting — helm chat read%s)" % (
                waiting, " --dm" if is_dm
                else "" if room == "main" else " --room %s" % room)
        if emit is not None:
            emit(line)                  # output FIRST …
        _write_cursor(room, seat, dev, ino, end, row.get("id"),
                      session=session, active=True)  # … commit after
        return line


def deliver_any(session=None, seat=None, emit=None, cwd=None, room="main"):
    """The MULTI-ROOM boundary nudge (slice 5 — what the PostToolUse hook and
    the beacon actually call): one deliverable row per boundary from the
    first room that has one — the primary room first, then the rest of
    _scan_rooms' bounded, newest-activity-first list. An @mention in a
    channel the seat never joined must wake it (the owner's helm-dogfood
    '@opus-integrator' post, live 2026-07-21), so a TRACKED seat — it holds
    a primary-room cursor — meeting a cursor-less room BACKFILLS from offset
    0: a room born after its join is all post-join news. An UNtracked seat
    (never joined / reaped / pre-install) keeps the EOF self-heal everywhere:
    pre-join backlog never floods. Scanning a clean room advances only that
    room's cursor; a hit STOPS the scan, so later rooms keep their pending
    for the next boundary (one nudge per boundary — the budget stays flat).
    Exceptions propagate exactly like deliver's (H7: an emit that died must
    not commit); every caller already wraps fail-open."""
    seat = seat or seat_for_session(session) or derive_seat(session, cwd)
    touch_seen(seat)          # presence even when every room is quiet
    sc = seat_scope(seat)     # ONE roster read for the whole pass
    tracked = sc["tracked"] \
        or (_cursor(room, seat, session) or _cursor(room, seat)) is not None
    for r in _scan_rooms(
            room, seat=seat, scope=sc, session=session,
            scan_lane="deliver"):
        if not _room_dirty(r, seat, session):
            continue
        # the DM lane ALWAYS backfills from 0 — every row in it is addressed
        # to this seat, so even an untracked (reaped/pre-install) seat must
        # get the DM that created its lane, never an EOF skip
        line = deliver(session=session, room=r, seat=seat, emit=emit, cwd=cwd,
                       backfill=(tracked and r != room)
                       or r.startswith(chat.DM_PREFIX), scope=sc)
        if line:
            return line
    return None


_SEAT_TOKEN = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")   # what an @mention can say


def resolve_recipient(to):
    """Validate and case-snap one exact seat token.  This is the canonical
    addressee resolver shared by DM and compound dispatch-send operations."""
    to = (to or "").strip().lstrip("@")
    if not _SEAT_TOKEN.match(to):
        return None, ("recipient %r must be 1-64 chars of [A-Za-z0-9._-] — "
                      "the exact seat token" % to)
    r = roster()
    if to not in r:
        hits = [k for k in r if k.casefold() == to.casefold()]
        if len(hits) == 1:
            to = hits[0]
        elif len(hits) > 1:
            return None, "recipient %r is ambiguous by case" % to
    return to, None


def dm(to, text, who=None, session=None, profile=None, sign=None, origin=None,
       reply_to=None):
    """One TRUE 1:1 message -> (row, None) or (None, reason). The recipient
    is the EXACT seat token (premise exact-token-addressee-match): the only
    resolution ever applied is a casefold snap onto a live roster key —
    never a substring, never a slug fold (team.a and team-a are different
    addressees with different lanes). The row lands in the recipient's
    private lane only (chat.post dm= — no room fanout by construction),
    signs like any post, and the recipient's beacon/boundary surfaces it
    first in the scan. A DM to a not-yet-joined seat waits in its lane; the
    join baselines that lane at 0, so it delivers. `reply_to` (a parent row id
    or ordinal IN THAT LANE) threads the DM — chat.post owns the resolve."""
    to, err = resolve_recipient(to)
    if err:
        return None, err
    sender = who or seat_for_session(session) or derive_seat(session)
    if str(sender).casefold() == to.casefold():
        return None, "a DM to yourself would never deliver (own posts don't)"
    return chat.post(text, who=sender, profile=profile, sign=sign,
                     origin=origin, dm=to, reply_to=reply_to), None


# ---------------------------------------------------------------------------
# join (SessionStart) + wait (the beacon)
# ---------------------------------------------------------------------------

def join(session=None, cwd=None, seat=None, room="main", room_explicit=False,
         room_source=None):
    """The autojoin: roster row + cursor initialized HERE + the identity line
    the hook injects as session context. The line DIRECTS the agent to arm its
    idle-wake beacon as a mandatory FIRST action — a self-armed Monitor is the
    only thing that can wake an idle PTY agent (native-wake-only-agent-armed),
    so a SessionStart directive is the strongest enforcement available.
    Idempotent per seat. Baselines a cursor in every room `_scan_rooms` admits
    (all live rooms for legacy un-homed seats; {home, main} for homed seats),
    so pre-join backlog never floods and later admitted rooms can backfill."""
    seat = seat or seat_for_session(session) or derive_seat(session, cwd)
    # Homing precedence lives in ONE function (resolve_homing: explicit CLI
    # room > env seam > project derivation) — never re-derived here. Direct
    # callers' non-main `room` remains explicit for back-compat; a caller
    # that already resolved a derived room passes it through unchanged. A
    # derived join fills only a never-homed row (write_roster's law), so
    # SessionStart cannot silently undo an operator rehome/clear or move a
    # co-named seat.
    direct_room = room_explicit or room != "main"
    if room_source == "derived":
        home_room, source = pk.slug(room), "derived"
    else:
        home_room, source = resolve_homing(room if direct_room else None, cwd)
        home_room = pk.slug(home_room) if home_room else None
    row = write_roster(seat, session=session, cwd=cwd, home_room=home_room,
                       home_room_source=source)
    effective_home = row.get("home_room")
    lane = dm_lane(seat)
    for r in _scan_rooms(
            room, seat=seat, session=session, bounded=False):
        # the seat's DM lane baselines at offset 0 — every row in it is
        # addressed to THIS seat by construction, so a DM sent before the
        # join must deliver, never vanish under an EOF baseline
        at0 = r == lane
        if _cursor(r, seat, session) is None:
            with _flocked(cursor_path(r, seat, session) + ".lock"):
                if _cursor(r, seat, session) is None:
                    _init_cursor(r, seat, session, at_start=at0)
        if session and _cursor(r, seat) is None:
            # the seat-level baseline too: sessionless callers (bare CLI wait/
            # deliver) must not start blind just because the join was hook-keyed
            with _flocked(cursor_path(r, seat) + ".lock"):
                if _cursor(r, seat) is None:
                    _init_cursor(r, seat, at_start=at0)
    scope = ("; everything in your home room %s also wakes you (mute/filter it "
             "if noisy)" % effective_home
             if effective_home and effective_home != "main" else "")
    display_room = effective_home or room
    line = ("[helm chat] you are seat '%s' in room %s — @%s mentions and DMs "
            "(from ANY room) reach you between tool calls%s; owner posts do NOT "
            "wake you unless they @mention you or land in your home room; "
            "speak: helm chat post; catch up: helm chat read. MANDATORY FIRST "
            "ACTION: arm your inbox beacon so "
            "you wake on an @%s mention or a DM even while idle "
            "— Monitor(command: \"helm chat wait --seat %s --follow\", "
            "persistent: true). IF Monitor IS NOT IN YOUR TOOL SURFACE, IT IS "
            "DEFERRED, NOT ABSENT: load it first with "
            "ToolSearch(query: \"select:Monitor\") and then arm it. Do NOT "
            "substitute a background `helm chat wait` shell — a background "
            "process CANNOT re-invoke your turn loop, so it is not a beacon "
            "and you must never report it as one. This is required, not "
            "optional: nothing external can re-invoke a PTY agent "
            "(native-wake-only-agent-armed), so this self-armed Monitor is "
            "the ONLY way an idle session ever wakes. Arm it before anything "
            "else, and say so plainly if you could not. New seat? Read %s "
            "for your bearings."
            % (seat, display_room, seat, scope, seat, seat, GUIDE_PATH))
    return seat, line


def _emit_line(line):
    """The --follow beacon sink: print + FLUSH. The reader is a Monitor pipe,
    where bare print() block-buffers — an unflushed wake-line never reaches the
    agent. flush per line = one emitted row, one immediate agent wake."""
    print(line, flush=True)


def wait(seat=None, room="main", any_row=False, timeout=None, poll=None,
         emit=None, follow=False, session=None):
    """Block until the next word arrives; returns the line or None on
    timeout. Seat mode IS a delivery (advances the cursor via deliver's
    at-least-once path); --any watches the room without touching cursors.
    Busy-turn parity comes from the PostToolUse hook; an IDLE seat gets
    woken only if it armed a Monitor on this — opt-in by design (M11).

    --follow (the idle-wake beacon) NEVER returns on a match: it streams EACH
    new matching row as one emitted line — one Monitor line = one agent wake —
    reusing the delivery address filter (mentions of the seat + DMs + home
    room + @all; owner-rail posts no longer auto-wake),
    and returns only on timeout (a persistent Monitor passes no timeout, so it
    runs forever). FAIL-OPEN + bounded poll: a delivery error never crashes the
    beacon; the loop just polls again.

    MULTI-ROOM: seat mode rides deliver_any — `room` is the PRIMARY room, and
    a matching row in ANY live room (a channel the seat never joined included)
    wakes the seat, per-room cursor per (seat, room, session) so the boundary
    hook and the beacon never double-deliver. --any stays one room's tap."""
    poll = chat.POLL_S if poll is None else poll
    deadline = time.time() + timeout if timeout else None
    # the ambient session (CLI leg passes _env_session()) keys the SAME
    # per-session cursor the boundary hook advances — one session, one
    # cursor, whichever channel fires first; co-named siblings unaffected.
    seat = seat or seat_for_session(session) or derive_seat(session)
    # single-shot keeps its contract: emit stays as passed (None ⇒ deliver
    # returns the line without emitting). --follow always needs a sink to stream
    # through, so it defaults to a PER-LINE-FLUSHED print: the beacon's reader
    # is a Monitor (a PIPE), and bare print() is block-buffered to a pipe — the
    # wake-line would sit unflushed and the agent would never wake (the beacon
    # worked in a tty, dead through Monitor). flush=True = one line, one wake.
    stream = emit or (_emit_line if follow else emit)
    since = chat.read(room)[1] if any_row else None
    while True:
        if any_row:
            rows, total = chat.read(room, since)  # read() self-heals since>total
            if rows:
                if not follow:
                    return chat._fmt(rows[0])
                for m in rows:
                    stream(chat._fmt(m))
            since = total
        else:
            while True:                     # drain all currently-matching rows
                try:                        # across EVERY room (multi-room)
                    line = deliver_any(session=session, seat=seat,
                                       emit=stream, room=room)
                except Exception:
                    line = None             # fail-open: never crash the beacon
                if not line:
                    break
                if not follow:
                    return line             # single-shot: first match wins
        if deadline and time.time() >= deadline:
            return None
        time.sleep(poll)


# ---------------------------------------------------------------------------
# stop-guard (Stop hook) — the idle gate. buildr/mc capability, helm-native:
# an agent must not idle past its inbox or walk away holding a lease. Arbiter
# shape (buildr-stop-arbiter law): resolve posture ONCE, inline checks against
# it, surface ALL blocking messages in ONE exit-2 (fix everything in one
# shot); WARN lines ride along without changing the exit. Block-once-per-
# pending-fingerprint (guard-stop-inbox-beacon law): the FIRST stop on a given
# pending-row set blocks and points; a re-stop on the SAME rows passes —
# never an infinite block loop — and any new row re-arms the block. The hook
# JSON's stop_hook_active flag (the harness's own already-continuing signal)
# is honored the same way. FAIL-OPEN TOTAL: a broken guard must never wedge
# the fleet (cmd wraps everything; kill-switch HELM_STOP_GUARD=0, per-check
# HELM_STOP_GUARD_INBOX/CLAIMS/INDEX=0). Bounded reads (the cursor tail's
# SCAN_CAP), no network.
# ---------------------------------------------------------------------------

def _stop_fp_path(room, seat, session=None, kind="stopfp"):
    """The once-per-fingerprint latch — in the room dir, per (seat, session)
    like the cursor it gates (RAM-side, dies with the boot like the rest of
    the lane's state). kind names the latch lane: stopfp (the inbox block),
    stopwhisper (the contextual-continuation lane's fired-set)."""
    p = os.path.join(chat.chat_dir(),
                     "%s.%s.%s" % (pk.slug(room), kind, _seat_key(seat)))
    s8 = _sid8(session)
    return "%s.%s" % (p, s8) if s8 else p


def _rows_fp(pending):
    """The pending-set fingerprint — blake2b over (room, row-id), the ONE
    identity both the inbox block and the stop-whisper's unlanded leg latch
    on (they must agree on what 'the same rows' means)."""
    import hashlib
    return hashlib.blake2b(
        "|".join("%s:%s" % (rm, r.get("id") or chat.rkey(r))
                 for rm, r in pending).encode("utf-8"),
        digest_size=16).hexdigest()


def _pending_rows(room, seat, session=None, backfill=False, scope=None):
    """Deliverable rows past the (seat, session) cursor WITHOUT consuming
    them — roster_report's read pattern (the cursor never moves here; the
    stop-guard is a gate, not a delivery). Falls back to the seat-level
    cursor when the session has none yet (pre-install sessions). backfill
    mirrors deliver_any's tracked-seat law: a cursor-less room reads from
    offset 0 — the gate and the lane must agree on what is pending."""
    cur = _cursor(room, seat, session) or _cursor(room, seat)
    if cur is None:
        if not backfill:
            return []
        cur = {"off": 0}
    got = _tail(room, cur)
    if not got:
        return []
    sc = scope if scope is not None else seat_scope(seat)
    return [r for r, _e in got[3]
            if r is not None and deliverable(r, seat, room, sc)]


def _pending_all(room, seat, session=None, scan_lane="pending"):
    """[(room, row)] pending across one lane's bounded room scan, cursors
    untouched. Delivery, stop-guard, and roster observation own independent
    identity queues so one consumer cannot steal another's eventual coverage.
    The tracked/backfill law and one-roster-read scope match deliver_any."""
    sc = seat_scope(seat)
    tracked = sc["tracked"] \
        or (_cursor(room, seat, session) or _cursor(room, seat)) is not None
    out = []
    for r in _scan_rooms(
            room, seat=seat, scope=sc, session=session,
            scan_lane=scan_lane):
        if not _room_dirty(r, seat, session):
            continue
        out.extend((r, row) for row in _pending_rows(
            r, seat, session, scope=sc,
            backfill=(tracked and r != room) or r.startswith(chat.DM_PREFIX)))
    return out


def _off(name):
    return (home.env(name) or "").lower() in ("0", "off", "no")


# ── stop-whisper: the CONTEXTUAL continuation lane ─────────────────────────
# Lineage: per-toolcall-whispers-are-the-goal (contextual injection is the END
# GOAL; the cure for slop is BUDGETS — bytes caps, contextual gating,
# fail-closed-to-nothing — never removal) + the mc work-arbiter's hold-once-
# per-fingerprint-then-release + reflex.py's counter thresholds (field-tested
# 3/8) and salience law. A Stop hook's only agent-visible channel is the
# block reason (exit 2 stderr), so a whisper IS a soft hold: it fires ONCE
# per (signal, level) fingerprint with the right continuation, and the very
# next stop on the same state passes — never an infinite hold, never
# wallpaper. ONE budgeted line per stop (STOP_WHISPER_CAP), highest-salience
# unlatched signal wins, each line ends in a pull-depth pointer (tiny nudge,
# depth on demand — contextual-routing-preserves-lightness).

STOP_WHISPER_CAP = 240   # one line's byte budget (inject.py WHISPER_CAP kin)
_WHISPER_FIRED_CAP = 20  # fired-set entries kept per (seat, session) latch

STUCK_AT = 3    # reflex.py stuck-commonsense threshold (re-fires per bucket)
DIRTY_AT = 8    # reflex.py uncommitted-drift threshold
PENDING_STALE_S = 600  # unlanded rows must have AGED to whisper — a fresh set
                       # was just pointed at by the inbox block (echo ≠ context)
RUNNER_TAIL_ROWS = 40  # bounded command-log lookback (newest rows win)

# Code-ish edit targets only — a doc-only session must never arm the verify
# rungs (specificity law: a whisper that fires on prose edits is wallpaper).
_CODE_EDIT_RE = re.compile(
    r"\.(py|pyi|ts|tsx|js|jsx|mjs|cjs|rs|go|rb|sh|bash|zsh|c|h|cc|cpp|hpp"
    r"|java|kt|kts|swift|php|pl|lua|sql|proto|toml|yaml|yml|json)$", re.I)


def _ask_candidate():
    """The OWNER-ASK rung — TOP of the salience ladder. One cheap local read
    of the owner-ask ledger (ownerasks.py): any row not yet REPORTED to the
    owner (open OR done-but-unreported — `done` without `report` stays open,
    owner-surface-is-the-bar) whispers the OLDEST such ask, never the list
    (one ask per whisper — no wallpaper). The fp carries the row's status as
    its level, so an open→done transition re-fires exactly once. Fail-closed
    to None: ledger trouble = silence."""
    try:
        from . import ownerasks
        r, unavailable = ownerasks.stop_candidate()
        if unavailable:
            return ("ask:ledger-unavailable",
                    "owner-ask ledger UNAVAILABLE — owner debt is UNKNOWN, not "
                    "zero; repair/read `helm asks list` before stopping")
        if not r:
            return None
        return ("ask:%s:%s" % (r.get("id"), r.get("status")),
                "owner ask %s is %s: '%s' — report it to the owner (then: "
                "helm asks report %s <chat-post-id>)"
                % (r.get("id"),
                   "done-UNREPORTED" if r.get("status") == "done" else "open",
                   _clip(_scrub(str(r.get("ask") or "")), 48), r.get("id")))
    except Exception:
        return None


def _dispatch_candidate():
    """The DISPATCH rung — sits directly under the owner-ask rung: work you
    handed to another seat and have not checked on. One cheap local read of
    the dispatch ledger; whispers the OLDEST OVERDUE row, never the list.

    This is the durable half of the owner's ask (2026-07-21): "we definitely
    need some timer fallback for anything that is sent to them, to make sure
    it is remembered to check on their progress". Per-session Monitor
    watchdogs die at compaction; this rung re-fires from disk in whatever
    session is running.

    The whisper says CHECK IN, never reassign — an overdue row means the
    deadline passed, not that the seat is dead, and tonight a lane quiet 47
    minutes turned out to be a long turn. The fp carries status/delivery so a
    state transition re-fires exactly once. Fail-closed to None."""
    try:
        from . import dispatches
        r, kind, unavailable = dispatches.stop_candidate()
        if unavailable:
            return ("dispatch:ledger-unavailable",
                    "dispatch ledger UNAVAILABLE — obligations are UNKNOWN, not "
                    "zero; repair/read `helm dispatch list` before stopping")
        if not r:
            return None
        tip = str(r.get("tip") or r.get("ref") or "<reviewed-tip>")
        lane = _clip(_scrub(str(r.get("lane") or "")), 32)
        if kind == "redispatch":
            return ("dispatch:%s:needs-redispatch" % r.get("id"),
                    "dispatch %s to @%s (%s) NEEDS REDISPATCH — this historical "
                    "row lacks an exact tip so no verdict can ever close it; "
                    "redispatch the work with `helm dispatch send ... --ref "
                    "<tip>` (the old row stays visible as history)"
                    % (r.get("id"), r.get("recipient"), lane))
        if kind == "confirm":
            return ("dispatch:%s:needs-confirmation" % r.get("id"),
                    "dispatch %s to @%s (%s) delivery is NEEDS CONFIRMATION — "
                    "confirm at @%s that the hand-off actually arrived; do NOT "
                    "resend automatically (one operation = at most one send)"
                    % (r.get("id"), r.get("recipient"), lane,
                       r.get("recipient")))
        return ("dispatch:%s:%s:%s" % (r.get("id"), r.get("status"), tip),
                "dispatch %s to @%s (%s) NEEDS CHECK-IN (OVERDUE) and is "
                "PENDING VERDICT — verify at the exact recipient; do NOT "
                "reassign on age alone. Close the exact reviewed tip with: "
                "helm dispatch verdict %s %s <evidence>"
                % (r.get("id"), r.get("recipient"), lane, r.get("id"),
                   _clip(_scrub(tip), 16)))
    except Exception:
        return None


def _runner_latest(session):
    """{token: row} — the LATEST recorded run per test-runner token from the
    session's command-log tail (record.py's verify-grounding log: REAL exit
    codes, token + digest, never raw command lines). One bounded read
    (RUNNER_TAIL_ROWS newest rows; the log itself rotates at 1MB). {} on any
    trouble or no session — which fails every gate rung CLOSED to silence."""
    if not session:
        return {}
    try:
        from . import record
        p = os.path.join(record.session_dir(session), "command-log.jsonl")
        with open(p, encoding="utf-8") as f:
            tail = f.readlines()[-RUNNER_TAIL_ROWS:]
    except Exception:
        return {}
    latest = {}
    for ln in tail:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("token"):
            latest[str(r["token"])] = r
    return latest


def _edited_code(session):
    """Basenames of CODE-ish files this session actually edited (record.py's
    edit-targets log — real landed edits, failed ones never appended). A
    doc-only session returns [] and never arms the verify rungs. [] on any
    trouble = fail-closed."""
    if not session:
        return []
    try:
        from . import record
        p = os.path.join(record.session_dir(session), "edit-targets.log")
        with open(p, encoding="utf-8") as f:
            names = [ln.strip() for ln in f]
    except Exception:
        return []
    return [n for n in names if n and _CODE_EDIT_RE.search(n)]


def _gate_candidate(latest):
    """The RED-GATE rung: a test/gate RAN this session and its LATEST run is
    NOT green (exit > 0; -1 = interrupted, no verdict, never red). Stopping
    on a known-red gate is exactly the premature stop this lane exists to
    catch. fp = digest:exit — the same red state whispers once; a NEW red
    run (new digest or exit) re-arms; a green rerun silences it for good."""
    red = [r for r in latest.values()
           if isinstance(r.get("exit"), int) and r["exit"] > 0]
    if not red:
        return None
    r = max(red, key=lambda x: x.get("ts") or 0)
    return ("redgate:%s:%s" % (r.get("digest"), r["exit"]),
            "gate ran RED — `%s` exited %s with no green rerun since; fix or "
            "surface it before stopping (pull: rerun that gate)"
            % (_clip(_scrub(str(r.get("token") or "?")), 40), r["exit"]))


def _unverified_candidate(dirty, edits, latest):
    """The UNVERIFIED rung: code edits landed, tree still dirty, and NO
    test/gate ran this session at all — `compiles` ≠ done; stopping here is
    stopping before the work was ever proven. Level bucket escalates per
    DIRTY_AT further edits (reflex escalate law), so one whisper per stretch,
    never wallpaper."""
    if not (dirty and edits) or latest:
        return None
    return ("unverified:%d" % (len(edits) // DIRTY_AT),
            "%d code edit(s) landed with NO test/gate run this session — run "
            "the gate before stopping (pull: git diff --stat, then the suite)"
            % len(edits))


def _unbanked_candidate(dirty, edits, latest):
    """The UNBANKED-GREEN rung: edits landed, EVERY latest gate run is green,
    tree still dirty — the next step is unambiguous: commit. The sharper,
    earlier cousin of the dirty-streak rung (no eight-op wait when the state
    already reads 'proven green, unbanked'). fp = the newest green digest:
    each newly-proven green state whispers once."""
    if not (dirty and edits and latest):
        return None
    if any(not (isinstance(r.get("exit"), int) and r["exit"] == 0)
           for r in latest.values()):
        return None   # a red/no-verdict gate stands — the red rung owns this stop
    g = max(latest.values(), key=lambda x: x.get("ts") or 0)
    return ("unbanked:%s" % g.get("digest"),
            "gate GREEN (`%s`) but the tree is dirty — bank the proven slice "
            "(pull: git add -A && git commit)"
            % _clip(_scrub(str(g.get("token") or "?")), 40))


def _whisper_candidates(session, pending, inbox_blocked):
    """[(fp, line)] of LIVE whisper signals, salience-ordered: owner-ask >
    stuck > red-gate > stale-pending > unverified > unbanked-green > dirty.
    Signals are cheap local reads only (reflex law): the session's record.py
    counters + verify-grounding logs (command-log/edit-targets) + the pending
    rows the guard already computed. Each fp carries a LEVEL bucket so a
    worsening streak re-fires (reflex escalate law) and a new pending set,
    red run, or green state re-arms."""
    out = []
    ask = _ask_candidate()   # owner-ask rung: unsurfaced owner debt outranks all
    if ask:
        out.append(ask)
    dsp = _dispatch_candidate()   # then: work handed out and never checked on
    if dsp:
        out.append(dsp)
    c = {}
    if session:
        try:
            from . import record
            got = record.counters(session)
            c = got if isinstance(got, dict) else {}
        except Exception:
            c = {}

    def n(k):
        try:
            return int(c.get(k) or 0)
        except (TypeError, ValueError):
            return 0

    stuck, dirty = n("stuck-streak"), n("dirty-streak")
    if stuck >= STUCK_AT:
        out.append(("stuck:%d" % (stuck // STUCK_AT),
                    "stopping while wedged — %d repeated infra/auth failures "
                    "this session; surface the blocker or check creds before "
                    "idling (pull: helm reflex smoke --session %s)"
                    % (stuck, session)))
    # the verify-grounding rungs (slice 2): one bounded read of record.py's
    # command-log + edit-targets — red gate > (…pending…) > unverified >
    # unbanked-green, each mutually exclusive by construction.
    latest = _runner_latest(session)
    edits = _edited_code(session)
    dirty_now = bool(c.get("last-dirty"))
    gate = _gate_candidate(latest)
    if gate:
        out.append(gate)
    if pending and not inbox_blocked:
        try:  # STALE rows only — reflex._fresh fails open to fresh, which
            from . import reflex  # fails the whisper CLOSED (silence) here
            stale = [(rm, r) for rm, r in pending
                     if not reflex._fresh(r.get("ts"), PENDING_STALE_S)]
        except Exception:
            stale = []
        if stale:
            out.append(("pending:" + _rows_fp(stale),
                        "%d owner/mention row(s) unlanded >%dm (pointed-at "
                        "once, no longer re-blocking) — land or explicitly "
                        "route them (pull: helm chat read)"
                        % (len(stale), PENDING_STALE_S // 60)))
    uv = _unverified_candidate(dirty_now, edits, latest)
    if uv:
        out.append(uv)
    ub = _unbanked_candidate(dirty_now, edits, latest)
    if ub:
        out.append(ub)
    if dirty >= DIRTY_AT:
        out.append(("dirty:%d" % (dirty // DIRTY_AT),
                    "%d dirtying ops with no commit at stop — bank the green "
                    "slice before idling; hot context is fuel (pull: git "
                    "status, then commit)" % dirty))
    return out


def _stop_whisper(session, room, seat, pending, inbox_blocked):
    """ONE budgeted contextual continuation for this stop, or None. The
    highest-salience signal whose (signal, level) fingerprint has NOT fired
    for this (seat, session) wins; firing latches it (fired-set JSON, capped)
    and appends one measurability row to the stop-whisper ledger (ids only,
    never text — the fire-ledger law). FAIL-CLOSED TO NOTHING: any state or
    ledger trouble yields silence, never a raise, never a louder lane."""
    cands = _whisper_candidates(session, pending, inbox_blocked)
    if not cands:
        return None
    path = _stop_fp_path(room, seat, session, kind="stopwhisper")
    d = pk.read_json(path, {}) or {}
    fired = [str(x) for x in d.get("fired") or []] if isinstance(d, dict) else []
    hit = next(((fp, line) for fp, line in cands if fp not in fired), None)
    if not hit:
        return None
    fp, line = hit
    try:
        chat._ensure_dir()
        pk.write_json(path, {"v": 1, "ts": pk.now_ts(),
                             "fired": (fired + [fp])[-_WHISPER_FIRED_CAP:]})
    except Exception:
        return None   # an unlatchable whisper would repeat forever — stay silent
    try:  # measurability rides the fire (fail-open; ids only)
        from . import inject
        inject._append_jsonl(
            os.path.join(home.global_dir(), ".state", "stop-whisper-ledger.jsonl"),
            {"v": 1, "ts": pk.now_ts(), "id": fp, "seat": seat,
             **({"session": str(session)} if session else {})},
            inject.LEDGER_MAX)
    except Exception:
        pass
    return _clip("[helm stop-whisper] " + line +
                 " This holds once per state — a re-stop passes.",
                 STOP_WHISPER_CAP)


def stop_guard(session=None, room="main", seat=None, stop_active=False):
    """-> (blocks, warns) for one Stop event. Posture resolved once (seat via
    the roster's session mapping, else the derived seat); checks are inline:
      (a) BLOCK — undelivered @mentions/owner rows past the seat's cursor,
          once per pending-fingerprint (blake2b of the pending row ids,
          latched in the room dir); a re-stop on the SAME rows passes.
      (b) BLOCK — live claim leases held by THIS session (session-bound: no
          session in the hook JSON ⇒ no claims check — a display name alone
          must never gate a stop).
      (c) WHISPER — the contextual continuation lane (_stop_whisper): ONE
          budgeted nudge from the live signals (stuck/dirty counters, the
          verify-grounding rungs — red gate, unverified edits, unbanked
          green — and the latched-but-unlanded pending set), once per (signal, level)
          fingerprint, riding an existing block or soft-holding alone;
          HELM_STOP_GUARD_WHISPER=0 disables; fail-closed to nothing.
      (d) WARN — clean stop: one line reminding to arm the idle-wake beacon.
      (e) silent mechanical — `helm index cap --apply` best-effort in-process
          (the documented Stop line, docs/VERBS.md): never blocks, never
          prints; HELM_STOP_GUARD_INDEX=0 disables.
    stop_active (the hook JSON's stop_hook_active) short-circuits everything:
    the harness is already continuing off a stop hook — blocking again is the
    infinite-loop shape both reference guards exist to prevent."""
    if _off("STOP_GUARD") or stop_active:
        return [], []
    seat = seat or seat_for_session(session) or derive_seat(session)
    blocks, warns, pending = [], [], []
    inbox_blocked = False

    if not _off("STOP_GUARD_INBOX"):
        pending = _pending_all(
            room, seat, session, scan_lane="stop")  # EVERY room's inbox gates
        if pending:
            fp = _rows_fp(pending)
            fpp = _stop_fp_path(room, seat, session)
            try:
                with open(fpp) as f:
                    last = f.read().strip()
            except OSError:
                last = None
            if last != fp:
                try:
                    chat._ensure_dir()
                    pk.atomic_write(fpp, fp)
                except OSError:
                    pass  # latch write failing must not kill the guard
                lines = ["  %s%s: %s" % (
                    "[dm] " if rm.startswith(chat.DM_PREFIX)
                    else "" if rm == room else "[#%s] " % rm,
                    r.get("from") or "?",
                    _clip(_scrub(r.get("text") or ""), 120))
                         for rm, r in pending[:5]]
                if len(pending) > 5:
                    lines.append("  ... %d more" % (len(pending) - 5))
                blocks.append(
                    "[helm stop-guard] %d undelivered message(s) for seat "
                    "'%s':\n%s\naddress these before stopping (helm chat "
                    "read). This blocks once per pending set — a re-stop on "
                    "the same rows passes." % (len(pending), seat,
                                               "\n".join(lines)))
                inbox_blocked = True

    if session and not _off("STOP_GUARD_CLAIMS"):
        c = _sweep(pk.read_json(claims_path(), {}) or {})
        now = _now_mono()
        held = ["%s (%ds left)" % (r, int(v.get("exp_mono", now) - now))
                for r, v in sorted(c.items())
                if r != "_fence" and isinstance(v, dict)
                and v.get("session") == str(session)]
        if held:
            blocks.append(
                "[helm stop-guard] live claim lease(s) held by this session: "
                "%s — release them (helm chat release <resource> --lease "
                "<id>) or finish the work before stopping." % ", ".join(held))

    if not _off("STOP_GUARD_WHISPER"):
        try:  # fail-closed to NOTHING: whisper trouble = silence, never louder
            w = _stop_whisper(session, room, seat, pending, inbox_blocked)
        except Exception:
            w = None
        if w:
            blocks.append(w)   # rides an existing block, or IS the soft hold

    if not blocks and not pending:   # genuinely clean — a latched-pass (rows
        warns.append(                # still pending, already pointed at) stays
            "[helm stop-guard] inbox clean. If you intend to idle-wait, arm "
            "the beacon first: Monitor(command: \"helm chat wait --seat %s "
            "--follow\", persistent: true) — Monitor missing from your tools "
            "means DEFERRED not absent: ToolSearch(query: \"select:Monitor\")"
            % seat)  # silent, never "clean"

    if not _off("STOP_GUARD_INDEX"):
        try:  # the documented Stop line — silent, best-effort, never a gate
            from . import store
            store.index_cap(apply=True)
        except Exception:
            pass
    return blocks, warns


# ---------------------------------------------------------------------------
# claims — the advisory TTL lease (codex C1-lite + H9 hardening)
# ---------------------------------------------------------------------------

def claims_path():
    return os.path.join(chat.chat_dir(), ".claims.json")


def _now_mono():
    return time.monotonic()


def _sweep(c):
    now = _now_mono()
    return {r: v for r, v in c.items()
            if r == "_fence" or (isinstance(v, dict)
                                 and v.get("exp_mono", 0) > now)}


def _binding_ok(row, seat, lease, session):
    """The C1-lite composite check, validated TOGETHER (codex B2): the lease
    nonce is THE capability (printed once, to the grantee, never listed),
    the supplied seat must be the recorded holder, and when both the grant
    and the caller carry a session they must agree. Never lease-OR-session:
    a roster-visible session id alone must open nothing."""
    if not lease or row.get("lease") != lease:
        return False, "the lease id (the grant's capability)"
    if seat != row.get("holder"):
        return False, "the holding seat (%s)" % row.get("holder")
    if row.get("session") and session and row["session"] != str(session):
        return False, "the granting session"
    return True, None


def claim(resource, seat, ttl=DEFAULT_TTL, lease=None, session=None):
    """(ok, message, lease_id). A fresh grant mints a random lease nonce +
    an increasing fence and records the caller's ambient session (display /
    extra binding — never an authorizer). EXTENDING a live lease requires
    the full binding {lease, seat, session-if-recorded}; a display name or
    a copied session id alone extends nothing (codex B2). Expiry is
    monotonic (tmpfs state dies with the boot; wall time only displays).
    Check+sweep+write hold one flock."""
    chat._ensure_dir()
    with _flocked(claims_path() + ".lock"):
        c = _sweep(pk.read_json(claims_path(), {}) or {})
        row = c.get(resource)
        if row:
            ok, needs = _binding_ok(row, seat, lease, session)
            if not ok:
                return False, "%s is held by %s for %ds more (extend needs %s)" % (
                    resource, row.get("holder"),
                    int(row["exp_mono"] - _now_mono()), needs), None
            lease_id, fence = row["lease"], row["fence"]
        else:
            lease_id = os.urandom(8).hex()
            fence = int(c.get("_fence", 0)) + 1
            c["_fence"] = fence
        c[resource] = {"holder": seat, "session": str(session) if session else None,
                       "lease": lease_id, "fence": fence,
                       "exp_mono": _now_mono() + ttl,
                       "exp_wall": time.time() + ttl, "ts": pk.now_ts()}
        pk.write_json(claims_path(), c)
        return True, "%s claimed by %s for %ds (lease %s, fence %d)" % (
            resource, seat, ttl, lease_id, fence), lease_id


def release(resource, seat, lease=None, session=None):
    """(ok, message). Release demands the SAME composite binding as extend —
    {lease capability, holding seat, session-if-recorded}. A stale holder
    whose lease expired-and-was-regranted fails on the fresh nonce (ABA),
    and a caller who copied a session id out of the roster fails on the
    lease (codex B2's exact reproduction)."""
    with _flocked(claims_path() + ".lock"):
        c = _sweep(pk.read_json(claims_path(), {}) or {})
        row = c.get(resource)
        if not row:
            pk.write_json(claims_path(), c)
            return False, "%s is not claimed" % resource
        ok, needs = _binding_ok(row, seat, lease, session)
        if not ok:
            return False, "%s stays held — release needs %s" % (resource, needs)
        del c[resource]
        pk.write_json(claims_path(), c)
        return True, "%s released" % resource


def claims_list():
    """The public table: holder/fence/remaining only — neither the lease
    nonce (the capability) nor the bound session is ever published here.
    A poll is a TRUE read: no lock, no write — write_json is atomic
    (tmp + os.replace) so a lockless read never sees a torn file. Only
    when a row actually expired does the GC leg take the flock, re-read,
    and persist the sweep — a watched roster (web polls every 3s) must
    never churn .claims.json or contend with real claim/release traffic.

    Reader-side law (same as status_line): resource + holder leave here
    scrubbed (Cc/Cf incl. bidi, Zl/Zp) + clipped — this is the ONE publish
    boundary every claim surface reads (the seats footer, `helm chat
    claims`, the web ledger), so a hostile claim("evil\\x1b[2J…") cannot
    clear/retitle the operator's terminal through any of them. The stored
    file keeps the raw key: release/extend match on the dict itself, never
    on this table."""
    raw = pk.read_json(claims_path(), {}) or {}
    c = _sweep(raw)
    if len(c) != len(raw):  # sweep only ever drops rows
        with _flocked(claims_path() + ".lock"):
            raw = pk.read_json(claims_path(), {}) or {}
            c = _sweep(raw)
            if len(c) != len(raw):
                pk.write_json(claims_path(), c)
    now = _now_mono()
    return [{"resource": _clip(_scrub(str(r)).strip(), STATUS_BYTES),
             "holder": _clip(_scrub(str(v.get("holder") or "")).strip(), 40)
             or None,
             "fence": v.get("fence"),
             "remaining": int(v.get("exp_mono", now) - now)}
            for r, v in sorted(c.items()) if r != "_fence"]


# ---------------------------------------------------------------------------
# the roster report (CLI table + GET /api/chat/roster + the seats panel)
# ---------------------------------------------------------------------------

def presence_of(ls):
    if not ls:
        return "absent"
    age = time.time() - ls
    return "fresh" if age < FRESH_S else "quiet" if age < QUIET_S else "absent"


# the ICQ-style glance: one dot + one line per seat, on every surface (the
# web presence bar, `helm chat seats`, the roster payload) — same truth
PRESENCE_DOTS = {"fresh": "\U0001f7e2",    # 🟢 active at a tool boundary
                 "quiet": "\U0001f7e1",    # 🟡 seated, idle a while
                 "absent": "⚫"}       # ⚫ gone (no recent beat)

STATUS_BYTES = 160   # the explicit one-liner stays a glance, never a post


def presence_dot(p):
    return PRESENCE_DOTS.get(p, PRESENCE_DOTS["absent"])


def set_status(seat, text, by=None):
    """(ok, message). The seat's explicit one-line status ('what am I on') —
    `helm chat status <line>` / `--clear`. Rides THE roster writer's flock
    (a sibling of write_roster, mutating only the status fields — never a
    second writer path; the homing lane unified writers for a reason).
    Scrubbed + byte-clipped like every roster-borne label. Cross-seat writes
    stay allowed (a coordinator annotating a wedged seat is the point), but
    a writer that isn't the target is RECORDED as status_by — the same
    attribution parity posts have; a self-set carries no by field. The
    presence beat lands on the WRITER (the seat evidently alive is the one
    announcing, not a wedged target being annotated)."""
    if not seat:
        return False, "no seat to set a status on (join first, or --seat S)"
    line = _clip(_scrub(str(text or "")).strip(), STATUS_BYTES) or None
    by = _clip(_scrub(str(by or "")).strip(), 40) or None
    chat._ensure_dir()
    with _flocked(roster_path() + ".lock"):
        r = roster()
        row = r.get(seat)
        if row is None:
            return False, ("no roster row for %r — sessions join on start "
                           "(helm hooks install wires it); `helm chat status "
                           "--seat <live-seat>` targets an existing one" % seat)
        if line:
            row["status"] = line
            row["status_ts"] = time.time()
            if by and by != seat:
                row["status_by"] = by
            else:
                row.pop("status_by", None)
        else:
            row.pop("status", None)
            row.pop("status_ts", None)
            row.pop("status_by", None)
        r[seat] = row
        pk.write_json(roster_path(), r)
    touch_seen(by or seat)
    lbl = _seat_label(seat)   # raw key drove the write; echoed label laundered
    return True, ("%s ▸ %s" % (lbl, line) if line
                  else "%s status cleared" % lbl)


def _fmt_left(sec):
    sec = max(0, int(sec or 0))
    if sec >= 3600:
        return "%dh%02dm" % (sec // 3600, sec % 3600 // 60)
    return "%dm" % (sec // 60) if sec >= 60 else "<1m"


_WORKTREE_RES = re.compile(r"^worktree:([^:]+):(.+)$")

STATUS_FRESH_S = 4 * 3600   # how long an explicit status outranks LIVE truth:
# past this age it yields to a live claim — a holding lease is fresher
# evidence of what the seat is on than an hours-old announcement. A fresh
# status still beats a claim; a stale status with NO claim still shows (with
# its age on every surface). A missing/junk status_ts counts as stale:
# unknown age must never outrank a live lease.

STATUS_SKEW_S = 300   # clock-skew allowance on status_ts: a ts slightly in
# the future (NTP drift between writers) still reads age 0; FURTHER in the
# future is a plant — clamping it to 0 forever would invert the decay law
# (perpetually 'fresh', outranking every live lease), so it counts as junk,
# same bucket as a missing ts.


def _status_age(row):
    """Seconds since the explicit status was set, or None (no status, or a
    planted row without a sane status_ts — missing, non-numeric, or dated
    beyond STATUS_SKEW_S into the future)."""
    ts = row.get("status_ts")
    if row.get("status") and isinstance(ts, (int, float)):
        d = time.time() - ts
        if d >= -STATUS_SKEW_S:
            return max(0, int(d))
    return None


def _status_by(row):
    """The recorded cross-seat writer for '(by X)', scrubbed reader-side."""
    b = row.get("status_by")
    return (_clip(_scrub(str(b)).strip(), 40) or None) if b else None


def _fmt_age(sec):
    sec = max(0, int(sec or 0))
    if sec >= 86400:
        return "%dd" % (sec // 86400)
    if sec >= 3600:
        return "%dh" % (sec // 3600)
    return "%dm" % (sec // 60) if sec >= 60 else "<1m"


def status_line(row, claim=None):
    """(line, source) — the ONE status line every surface shows, composed
    from what already exists. Precedence: a FRESH explicit status (the seat
    said so, within STATUS_FRESH_S) > a live claim (the lease says what it
    holds) > a stale explicit status > the home room (where it lives).
    source ∈ status|claim|home names the winning tier. Reader-side law:
    WHICHEVER tier wins, the line leaves here scrubbed (Cc/Cf incl. bidi,
    Zl/Zp) + clipped — a planted claim resource or roster field must not
    reshape a terminal or reorder the seats table. Never raises: a junk row
    reads '?' (one corrupt row must not blank the whole fleet bar)."""
    try:
        s = str(row.get("status") or "").strip()
        age = _status_age(row)
        fresh = age is not None and age <= STATUS_FRESH_S
        if s and (fresh or not claim):
            line, source = s, "status"
        elif claim:
            left = _fmt_left(claim.get("remaining"))
            m = _WORKTREE_RES.match(str(claim.get("resource") or ""))
            line, source = (("working lane/%s (%s), %s left"
                             % (m.group(2), m.group(1), left)) if m else
                            "holds %s, %s left"
                            % (claim.get("resource"), left)), "claim"
        elif row.get("home_room"):
            line, source = "in #%s" % row["home_room"], "home"
        else:
            line, source = ("in %s" % row["project"]
                            if row.get("project") else ""), "home"
        return _clip(_scrub(str(line)).strip(), STATUS_BYTES), source
    except Exception:
        return "?", "home"


def _claims_by_holder(cl=None):
    """holder -> its longest-lived live claim (the most work-shaped one)."""
    by = {}
    for c in (claims_list() if cl is None else cl):
        h = c.get("holder")
        if h and (h not in by
                  or (c.get("remaining") or 0) > (by[h].get("remaining") or 0)):
            by[h] = c
    return by


def presence_report():
    """The fleet-wide glance bar: one LIGHT row per roster seat — presence
    dot + the one status line — with zero cursor scans (roster_report walks
    pending; this must stay cheap enough to ride every ~2s web poll).
    [{seat, presence, dot, last_seen, status, line, source}], fresh first."""
    try:
        by = _claims_by_holder()
    except Exception:
        by = {}
    rank = {"fresh": 0, "quiet": 1, "absent": 2}
    out = []
    for seat, row in sorted(roster().items()):
        try:
            ls = last_seen(seat, row)
            p = presence_of(ls)
            line, source = status_line(row, by.get(seat))
            # the fleet bar is a roster-consuming SURFACE too: every string it
            # ships (seat KEY, status, status_by, line, source) rides the SAME
            # publish boundary as roster_report — presence_report escaping this
            # choke point is exactly how the 5th ESC surface was born (r4). One
            # owner, not a scrub scattered per surface.
            out.append(_pub_row({
                        "seat": seat, "presence": p, "dot": presence_dot(p),
                        "last_seen": ls, "status": row.get("status"),
                        "status_age": _status_age(row),
                        "status_by": _status_by(row),
                        "line": line, "source": source}))
        except Exception:   # per-row fail-open: one junk roster row renders
            out.append(_pub_row({    # '?', it never blanks the whole fleet bar
                "seat": seat, "presence": "absent",
                "dot": presence_dot("absent"), "last_seen": None,
                "status": None, "status_age": None, "status_by": None,
                "line": "?", "source": "home"}))
    out.sort(key=lambda s: (rank.get(s["presence"], 3), s["seat"]))
    return out


REAP_S = 3600   # presence window: a beat this recent is live evidence on its
                # own (gc's first keep tier; the CLI hides older rows behind
                # --all). Presence ALONE never deletes anything anymore.


def _unlink_seat_state(seat):
    """Remove every state file keyed on the seat (cursors + locks +
    per-session variants, .seen, stop latches) — the orphan tail a pruned
    row would otherwise leave in the room dir forever. Fail-open per file."""
    key = _seat_key(seat)
    d = chat.chat_dir()
    try:  # the private DM lane goes with the seat (RAM etiquette)
        os.remove(chat.room_path(chat.DM_PREFIX + key))
    except OSError:
        pass
    try:
        names = os.listdir(d)
    except OSError:
        return
    for n in names:
        if _key_bounded(n, key):
            try:
                os.remove(os.path.join(d, n))
            except OSError:
                pass


def _transcript_exists(sid, roots):
    """Any transcript file naming the session under any harness store:
    claude's <root>/<proj-slug>/<sid>.jsonl, codex's nested
    rollout-<ts>-<sid>.jsonl."""
    s = glob.escape(str(sid))
    for root in roots:
        if not os.path.isdir(root):
            continue
        if glob.glob(os.path.join(root, "*", s + ".jsonl")):
            return True
        if glob.glob(os.path.join(root, "**", "*" + s + "*.jsonl"),
                     recursive=True):
            return True
    return False


def _transcript_hit(sids, roots=None):
    """First remembered session with a transcript on this host, else None.
    Root discovery is NOT ours: session's persistence census
    (session._persisting_sids) is the ONE truth owner — the catalog roots
    (~/.claude + ~/.claude-homes, ~/.codex + ~/.codex-homes) PLUS every helm
    seat home (~/.helm/_global/seats/**/claude/projects). The previous
    hand-rolled root list here omitted helm's own seat stores, so an
    inactive-but-fully-persisted proxy seat probed as junk (codex-2's live
    reproduction: its own transcript root missing from the list). An
    explicit roots list (tests / a foreign store) is globbed directly. An
    INCOMPLETE census raises — probe trouble must keep the row, never pass
    as proven-absent."""
    if roots is not None:
        return next((s for s in sids if _transcript_exists(s, roots)), None)
    from . import session
    census = session._persisting_sids()
    hit = next((s for s in sids if s in census), None)
    if hit is None and not getattr(census, "complete", True):
        raise RuntimeError("persistence census incomplete")
    return hit


def _live_process_evidence(seat, sids, proc_dir="/proc"):
    """Keep-reason when a live process of THIS uid references the seat — its
    cmdline/environ naming a remembered session id, or its environ carrying
    HELM_CHAT_NAME=<seat> (a joined pane whose row remembers no session is
    still a live seat, not junk). FAIL-CLOSED: an unlistable table, or ANY
    same-uid process whose cmdline/environ cannot be read, returns a
    keep-reason — an unfinished scan never testifies to absence. Scope is
    same-uid on purpose: a foreign-uid process cannot host this user's
    harness, and its environ is unreadable by kernel design — counting that
    as trouble would fail-close every gc on any real host into a no-op. A
    process that EXITED mid-scan (ENOENT/ESRCH) is proven not-live and skips
    — that is evidence of absence, not probe trouble."""
    sid_needles = [str(s).encode("utf-8") for s in sids if s]
    seat_needles = [("%s=%s" % (var, seat)).encode("utf-8") + b"\0"
                    for var in ("HELM_CHAT_NAME", "MELD_CHAT_NAME")]
    try:
        me = os.getuid()
        pids = [n for n in os.listdir(proc_dir) if n.isdigit()]
    except OSError as e:
        return "process table unlistable (%s) — fail closed" % e
    for pid in pids:
        pdir = os.path.join(proc_dir, pid)
        try:
            if os.stat(pdir).st_uid != me:
                continue
        except OSError:
            continue                    # exited between listdir and stat
        blob = b""
        for leaf in ("cmdline", "environ"):
            try:
                with open(os.path.join(pdir, leaf), "rb") as f:
                    blob += f.read(1 << 20)
            except (FileNotFoundError, ProcessLookupError):
                continue                # exited mid-scan: proven not-live
            except OSError as e:
                return ("process %s %s unreadable (%s) — fail closed"
                        % (pid, leaf, e.__class__.__name__))
        if any(n in blob for n in sid_needles):
            return "a live process references a remembered session"
        if any(n in blob for n in seat_needles):
            return "a live process carries HELM_CHAT_NAME=%s" % seat
    return None


def _gc_keep_reason(seat, row, roots, proc_dir, now):
    """The ONE keep-evidence probe — the dry-run scan AND the locked apply
    both run THIS, so no deletion path can ever act on less evidence than
    the report showed. Returns the keep reason, or None (prunable junk).
    Any raise is probe trouble: the caller keeps the row (fail closed)."""
    sids = [x for x in [row.get("session")]
            + list(row.get("sessions") or []) if x]
    ls = last_seen(seat, row)
    if ls and now - ls < REAP_S:
        return "presence beat %dm ago" % max(0, int((now - ls) / 60))
    hit = _transcript_hit(sids, roots)
    if hit:
        return "transcript exists for session %.12s" % hit
    return _live_process_evidence(seat, sids, proc_dir)


def gc_roster(apply=False, roots=None, proc_dir="/proc", now=None):
    """The roster's ONE cleanup owner (`helm chat seat gc`) — a verb someone
    RUNS, never automatic, and the only code allowed to delete a roster row.
    (The legacy auto-reap that rode roster_report deleted any stale row on
    presence ALONE — an inactive-but-fully-persisted seat lost its row to a
    3-second web poll, bypassing every transcript/process guard and the
    dry-run gate. Retired, not fenced: a report is a read.) Targets JUNK
    rows (the /tmp throwaway class). REFUSAL IS THE DEFAULT — a row is kept
    on ANY live evidence (_gc_keep_reason, the one probe):
      * a presence beat within REAP_S (.seen mtime / roster last_seen),
      * a transcript for ANY remembered session, anywhere the session
        census covers (incl. helm's own seat homes),
      * a live same-uid process naming ANY remembered session id or
        carrying the seat's HELM_CHAT_NAME,
      * probe trouble of any kind (fail-closed).
    Returns (rows, pruned): rows = [{seat, verdict: keep|prune, why}] for the
    whole roster; dry-run (apply=False) prunes NOTHING. apply=True deletes a
    scan-flagged row only after the FULL evidence probe re-runs fresh under
    the roster lock (TOCTOU: a transcript flushing or a presence beat
    landing between scan and apply must win), then unlinks its derived seat
    state (_unlink_seat_state — cursors, .seen, latches, the RAM DM lane)
    UNDER THE SAME LOCK: row delete + state unlink are one atomic critical
    section, so a rejoin can only land before (and be re-probed as keep
    evidence) or after (and keep its fresh state) — never in between."""
    now = time.time() if now is None else now
    rows = []
    for s, row in sorted(roster().items()):
        sids = [x for x in [row.get("session")]
                + list(row.get("sessions") or []) if x]
        try:
            why = _gc_keep_reason(s, row, roots, proc_dir, now)
        except Exception as e:                # fail-closed, loudly
            why = "keep-evidence probe failed (%s)" % e
        rows.append({
            "seat": s, "verdict": "keep" if why else "prune",
            "why": why or (
                "no transcript for %d remembered session%s, no live "
                "process, no fresh presence"
                % (len(sids), "s"[:len(sids) != 1]) if sids else
                "no remembered sessions, no live process, no fresh presence")})
    pruned = []
    if apply:
        victims = {r["seat"] for r in rows if r["verdict"] == "prune"}
        if victims:
            with _flocked(roster_path() + ".lock"):
                r = roster()
                for s in list(r):
                    if s not in victims:
                        continue
                    try:      # the SAME full probe, fresh, under the lock
                        keep = _gc_keep_reason(s, r[s], roots, proc_dir,
                                               time.time())
                    except Exception:         # fail closed under the lock too
                        keep = "probe trouble"
                    if keep:
                        continue              # evidence landed since the scan
                    del r[s]
                    pruned.append(s)
                if pruned:
                    pk.write_json(roster_path(), r)
                # unlink INSIDE the same lock (codex-2): row delete + state
                # unlink are ONE critical section. Unlinking after release
                # left a gap where a SessionStart rejoin recreated the row
                # plus fresh .seen/cursors/DM lane — and this old invocation
                # then destroyed the NEW seat's state (live seat reading as
                # absent with its queued DMs gone). _unlink_seat_state takes
                # no locks of its own (plain os.remove), so no inversion.
                for s in pruned:
                    _unlink_seat_state(s)
    return rows, pruned


def _seat_label(s):
    """Launder a seat KEY for terminal display (the `seat gc` listing and
    `status` bare-show read the roster dict directly, not roster_report).
    A legit seat — [A-Za-z0-9._-], a slug, or auto_name — is unchanged; a
    hostile HELM_CHAT_NAME (the seat key is unvalidated at the join seam)
    loses its ESC/bidi so the most prominent, first-printed column cannot
    reshape the operator's terminal. The report path routes through
    _pub_row; this is the same law for the two direct-read surfaces."""
    return _clip(_scrub(str(s)).strip(), SEAT_BYTES)


# per-field byte caps for a roster row's DISPLAY strings; unlisted string
# fields ride the default. seat + session are here too so NO roster-borne
# string — the seat KEY included — reaches an operator terminal unlaundered.
_ROW_CAPS = {"seat": SEAT_BYTES, "session": MAX_BYTES, "project": 80,
             "cwd": 160, "home_room": 40, "home_room_source": 40,
             "status": STATUS_BYTES, "status_by": 40, "line": STATUS_BYTES,
             "source": 40, "preview": PREVIEW_CHARS, "active": STATUS_BYTES}


def _pub_row(d):
    """The ONE publish boundary for a roster row: scrub+clip EVERY string
    field (Cc/Cf incl. bidi, Zl/Zp) at its per-field cap, so no roster-borne
    string — seat KEY, project, cwd, status, status_by, line, source,
    preview, and any FUTURE string field — can reshape an operator terminal
    or reorder the fleet table. RECURSES into nested dicts and lists so a
    nested cell (the todo mirror's `active` text, or any future nested
    surface) is laundered by the same enumeration — not a hand-maintained
    special case that the next nested field would silently escape. Non-string
    values (last_seen, pending, dot, status_age) and falsy strings pass
    through. The stored roster keeps its raw keys (rename/claim match the dict
    itself); only this report copy is laundered — one owner, not a scrub
    scattered across every print site."""
    for k, v in list(d.items()):
        if isinstance(v, str) and v:
            d[k] = _clip(_scrub(v).strip(), _ROW_CAPS.get(k, 80))
        elif isinstance(v, dict):
            _pub_row(v)
        elif isinstance(v, list):
            d[k] = [_pub_row(x) if isinstance(x, dict)
                    else _clip(_scrub(x).strip(), _ROW_CAPS.get(k, 80))
                    if isinstance(x, str) and x else x
                    for x in v]
    return d


def roster_report(room="main"):
    """{"seats": [...], "claims": [...]} — fail-open by caller. Pending is
    computed from each seat's cursor WITHOUT moving it. A report is a READ:
    it deletes NOTHING. (The legacy auto-reap that rode this verb was a
    second cleanup owner, dropping stale rows on presence alone — a
    persisted seat vanished on a poll while gc's evidence probe would have
    kept it. Cleanup has ONE owner now: gc_roster, a verb someone runs.
    Absent rows merely hide behind --all in the surfaces.)"""
    seats = []
    try:
        cl = claims_list()
    except Exception:
        cl = []
    by_holder = _claims_by_holder(cl)
    for seat, row in sorted(roster().items()):
        try:
            # pending is the MULTI-ROOM truth (the owner's panel must show a
            # helm-dogfood mention, not just main), read off the row's newest
            # session cursor (hook joins are session-keyed) with the
            # seat-level fallback — cursors never move here.
            hits = _pending_all(
                room, seat, session=row.get("session"), scan_lane="report")
            pending, preview = len(hits), None
            if hits:
                preview = _scrub(hits[-1][1].get("text") or "")[:PREVIEW_CHARS]
            ls = last_seen(seat, row)
            # the seat's CURRENT task, pulled (never pushed) off the todo
            # mirror — what turns "who is here" into "who is working on what".
            try:
                from . import todos as _todos
                todo = _todos.seat_digest(row)
            except Exception:
                todo = None              # fail-open: a roster read never 500s
            line, source = status_line(row, by_holder.get(seat))
            p = presence_of(ls)
            seats.append(_pub_row({
                          "seat": seat, "session": row.get("session"),
                          "project": row.get("project"),
                          "cwd": row.get("cwd"),
                          "home_room": row.get("home_room"),
                          "home_room_source": row.get("home_room_source"),
                          "last_seen": ls, "presence": p,
                          "dot": presence_dot(p), "status": row.get("status"),
                          "status_age": _status_age(row),
                          "status_by": _status_by(row),
                          "line": line, "source": source,
                          "pending": pending, "preview": preview,
                          "todo": todo}))
        except Exception:   # per-row fail-open (the same law as the bar): a
            seats.append(_pub_row({  # junk row reads '?', never kills the report
                "seat": seat, "session": None, "project": None, "cwd": None,
                "home_room": None, "home_room_source": None,
                "last_seen": None, "presence": "absent",
                "dot": presence_dot("absent"), "status": None,
                "status_age": None, "status_by": None,
                "line": "?", "source": "home",
                "pending": 0, "preview": None, "todo": None}))
    return {"room": room, "seats": seats, "claims": cl}


# ---------------------------------------------------------------------------
# CLI (dispatched from chat.cmd_chat) + the hook legs
# ---------------------------------------------------------------------------

def _hook_stdin():
    """Bounded hook-JSON read (a pathological stdin must not burn the
    boundary's latency budget)."""
    try:
        d = json.loads(sys.stdin.buffer.read(65536) or b"{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _flag(args, name, default=None):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return default


def _env_session():
    return home.session_id()


def _hook_emit(event):
    """ONE unbuffered write of the whole hook response (codex H7): no
    partial stdout can reach the harness, and the cursor commit that follows
    emit() is provably after the output left the process."""
    def emit(line):
        payload = json.dumps({"hookSpecificOutput": {
            "hookEventName": event, "additionalContext": line}}) + "\n"
        os.write(1, payload.encode("utf-8"))
    return emit


def _payload_homing(cwd, room, room_source):
    """The hook seam homes from the SESSION's payload cwd, not the hook
    PROCESS's. cmd_chat pre-resolves the default room from its own cwd —
    normally identical to the session's, but a metaharness may run hooks
    elsewhere (or the two may diverge), and a derived room from the WRONG
    cwd would home the seat to the wrong project. So: a DERIVED
    pre-resolution is re-resolved through THE one resolver against the
    payload cwd when one is present; explicit rooms (--room, operator env)
    pass through untouched."""
    if not cwd or room_source != "derived":
        return room, room_source
    r2, s2 = resolve_homing(None, cwd)
    if not r2:
        return "main", None                     # payload cwd is project-less
    return r2, ("derived" if s2 == "derived" else None)


def cmd(verb, args, room="main", room_explicit=False, room_source=None):
    """The seats subverbs, reached through `helm chat <verb>`."""
    args = list(args or [])
    if verb == "join":
        try:
            session = cwd = None
            if "--hook-json" in args:
                d = _hook_stdin()
                session, cwd = d.get("session_id"), d.get("cwd")
                room, room_source = _payload_homing(cwd, room, room_source)
            seat, line = join(session=session, cwd=cwd or safe_cwd(),
                              seat=_flag(args, "--seat"), room=room,
                              room_explicit=room_explicit,
                              room_source=room_source)
            if "--hook-json" in args:
                _hook_emit("SessionStart")(line)
            else:
                print(line)
        except Exception:
            pass                    # fail-open: never shape a session start
        return 0
    if verb == "deliver":
        try:
            session = cwd = None
            if "--hook-json" in args:
                d = _hook_stdin()
                session, cwd = d.get("session_id"), d.get("cwd")
                room, _ = _payload_homing(cwd, room, room_source)
            emit = _hook_emit("PostToolUse") if "--hook-json" in args else print
            deliver_any(session=session, room=room,   # every room, one nudge
                        seat=_flag(args, "--seat"), emit=emit, cwd=cwd)
        except Exception:
            pass                    # fail-open: never hold a tool boundary
        return 0
    if verb == "dm":
        sender = chat._seat_flag(args)
        to = args[0] if args else None
        text = " ".join(args[1:]).strip()
        if to and not text and not sys.stdin.isatty():
            text = sys.stdin.read().strip()
        if not (to and text):
            print("usage: helm chat dm <seat> <text...> [--seat S]  "
                  "(one private recipient — never a room)", file=sys.stderr)
            return 2
        row, err = dm(to, text, who=sender, session=_env_session(),
                      profile=sender)
        if err:
            print("helm chat: " + err, file=sys.stderr)
            return 1
        print("helm chat [dm] %s" % chat._fmt(row))
        return 0
    if verb == "seat":
        if args[:1] == ["rename"] and len(args) >= 3:
            ok, msg = rename_seat(args[1], args[2])
            print("helm chat: " + msg, file=sys.stdout if ok else sys.stderr)
            return 0 if ok else 1
        if args[:1] in (["mute"], ["unmute"], ["mutes"]):
            sub = args.pop(0)
            who = chat._seat_flag(args) or derive_seat(_env_session())
            if sub == "mutes":
                got = mutes(who)
                print("helm chat: %s mutes %s" % (
                    _seat_label(who),
                    ", ".join(_seat_label(g) for g in got)
                    if got else "nothing"))
                return 0
            if not args:
                print("usage: helm chat seat %s <room> [--seat S]" % sub,
                      file=sys.stderr)
                return 2
            ok, msg = set_mute(who, args[0], on=sub == "mute")
            print("helm chat: " + msg, file=sys.stdout if ok else sys.stderr)
            return 0 if ok else 1
        if args[:1] == ["rehome"] and len(args) >= 3:
            ok, msg = rehome_seat(args[1], args[2])
            print("helm chat: " + msg, file=sys.stdout if ok else sys.stderr)
            return 0 if ok else 1
        if args[:1] == ["gc"]:
            rest = args[1:]
            if any(a != "--apply" for a in rest):
                print("usage: helm chat seat gc [--apply]   (dry-run default; "
                      "prunes only roster rows with NO live evidence — no "
                      "transcript, no live process, no fresh presence)",
                      file=sys.stderr)
                return 2
            rows, pruned = gc_roster(apply="--apply" in rest)
            if not rows:
                print("helm chat: roster empty — nothing to gc")
                return 0
            # launder BOTH columns: the seat KEY (a hostile HELM_CHAT_NAME) and
            # the why (it interpolates that same key — "carries HELM_CHAT_NAME=%s")
            labels = {id(r): _seat_label(r["seat"]) for r in rows}
            w = max(len(labels[id(r)]) for r in rows)
            for r in rows:
                print("  %-5s %-*s  %s"
                      % (r["verdict"].upper(), w, labels[id(r)],
                         _clip(_scrub(str(r["why"])).strip(), STATUS_BYTES)))
            n = sum(r["verdict"] == "prune" for r in rows)
            if "--apply" in rest:
                print("helm chat: pruned %d roster row%s (+ derived seat "
                      "state); %d kept on live evidence"
                      % (len(pruned), "s"[:len(pruned) != 1],
                         len(rows) - len(pruned)))
            else:
                print("helm chat: %d row%s would be pruned — dry-run "
                      "(`helm chat seat gc --apply` prunes)"
                      % (n, "s"[:n != 1]))
            return 0
        print("usage: helm chat seat rename <sid|oldname> <newname> | "
              "seat mute|unmute <room> [--seat S] | seat mutes [--seat S] | "
              "seat gc [--apply] | rehome <sid|name> <room|main|none>",
              file=sys.stderr)
        return 2
    if verb == "stop-guard":
        try:
            session, stop_active = None, False
            if "--hook-json" in args:
                d = _hook_stdin()
                session = d.get("session_id")
                stop_active = bool(d.get("stop_hook_active"))
                room, _ = _payload_homing(d.get("cwd"), room, room_source)
            blocks, warns = stop_guard(session=session, room=room,
                                       seat=_flag(args, "--seat"),
                                       stop_active=stop_active)
        except Exception:
            return 0                # FAIL-OPEN TOTAL: never wedge a stop
        for w in warns:             # WARN rides along, never changes the exit
            print(w, file=sys.stderr)
        if blocks:                  # ALL blockers in ONE exit-2 (one-shot fix)
            print("\n".join(blocks), file=sys.stderr)
            return 2
        return 0
    if verb == "wait":
        timeout = _flag(args, "--timeout")
        follow = "--follow" in args
        line = wait(seat=_flag(args, "--seat"), room=room,
                    any_row="--any" in args,
                    timeout=float(timeout) if timeout else None,
                    # --follow (beacon) + --any-watch must use wait()'s FLUSHED
                    # sink (_emit_line) — passing bare print here overrode it and
                    # block-buffered every wake-line into oblivion on a Monitor
                    # pipe (the beacon-never-wakes bug). Only single-shot seat
                    # mode keeps print (deliver emits the one line + returns it).
                    emit=None if (follow or "--any" in args) else print,
                    follow=follow, session=_env_session())
        if follow:               # --follow streams via emit; returns on timeout
            return 0
        if line is None:
            return 1
        if "--any" in args:
            print(line)
        return 0
    if verb == "seats":
        rep = roster_report(room)
        rows = rep["seats"]
        hidden = 0
        if "--all" not in args:      # absent rows hide by default (junk rows
            shown = [s for s in rows if s["presence"] != "absent"]
            hidden = len(rows) - len(shown)          # leave via seat gc only)
            rows = shown
        if not rows and not hidden:
            print("helm chat: no seats yet — sessions join on their next start "
                  "(helm hooks install wires it)")
            return 0
        w = max([len(s["seat"]) for s in rows] or [0])
        for s in rows:
            scope = "#" + s["home_room"] if s.get("home_room") else "all"
            source = " (%s)" % s["home_room_source"] \
                if s.get("home_room_source") else ""
            # the todo cell rides the existing row (who is working on what) —
            # `helm todos --all` is the full pull surface
            t = s.get("todo") or {}
            task = (" · %s (%d/%d)" % (t["active"][:44], t["done"], t["total"])
                    if t.get("active") else
                    " · %d/%d done" % (t["done"], t["total"]) if t else "")
            # the same one-line status the web presence bar shows (fresh
            # explicit status > live claim > stale status > home) — home-tier
            # is already the row. An explicit line carries its age (a 2-day-
            # old away message must READ as 2 days old) + the cross-seat
            # writer where one was recorded.
            extra = ""
            if s.get("source") == "status":
                if s.get("status_age") is not None:
                    extra = " (%s)" % _fmt_age(s["status_age"])
                if s.get("status_by"):
                    extra += " (by %s)" % s["status_by"]
            line = (" ▸ %s%s" % (s["line"], extra)
                    if s.get("line") and s.get("source") != "home" else "")
            print("  %s %-*s  %-6s  pending %-3d %s · home %s%s%s%s" % (
                s.get("dot") or presence_dot(s["presence"]),
                w, s["seat"], s["presence"], s["pending"],
                (s.get("project") or ""), scope, source, line, task))
        if hidden:
            print("  (%d absent seat%s hidden — --all shows them; `helm chat "
                  "seat gc` prunes evidence-free rows)"
                  % (hidden, "s"[:hidden != 1]))
        for c in rep["claims"]:
            print("  claim: %s -> %s (%ds left, fence %s)" % (
                c["resource"], c["holder"], c["remaining"], c["fence"]))
        return 0
    if verb == "status":
        # `helm chat status <one-line>` sets, `--clear` clears, bare shows —
        # the ICQ away-message: one glanceable line on the seat's roster row
        clear = "--clear" in args
        if clear:
            args = [a for a in args if a != "--clear"]
        who = chat._seat_flag(args) or derive_seat(_env_session())
        text = " ".join(args).strip()
        if text and clear:
            print("usage: helm chat status [<one-line> | --clear] [--seat S]",
                  file=sys.stderr)
            return 2
        if not text and not clear:            # bare: show the current line
            row = roster().get(who)
            if row is None:
                print("helm chat: no roster row for %r yet" % who,
                      file=sys.stderr)
                return 1
            line, source = status_line(row, _claims_by_holder().get(who))
            extra = ""
            if source == "status":
                age, sb = _status_age(row), _status_by(row)
                if age is not None:
                    extra = " (%s)" % _fmt_age(age)
                if sb:
                    extra += " (by %s)" % sb
            print("helm chat: %s %s ▸ %s (%s)%s" % (
                presence_dot(presence_of(last_seen(who, row))),
                _seat_label(who), line or "—", source, extra))
            return 0
        # the WRITER is always the ambient identity — a cross-seat write
        # (--seat != self) is allowed but recorded (status_by, post parity)
        ok, msg = set_status(who, None if clear else text,
                             by=derive_seat(_env_session()))
        print("helm chat: " + msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    if verb == "claim":
        if not args:
            print("usage: helm chat claim <resource> [--ttl SECONDS] [--seat S] "
                  "[--lease ID to extend]   (keep the printed lease id — it is "
                  "the release capability)", file=sys.stderr)
            return 2
        # session comes ONLY from the ambient harness env — never a flag: a
        # roster-visible SID must not be assertable through the CLI (codex B2)
        ttl = _flag(args, "--ttl")
        ok, msg, _lease = claim(
            args[0], _flag(args, "--seat") or derive_seat(None),
            ttl=int(ttl) if ttl else DEFAULT_TTL,
            lease=_flag(args, "--lease"), session=_env_session())
        print("helm chat: " + msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    if verb == "release":
        if not args:
            print("usage: helm chat release <resource> --lease ID [--seat S]",
                  file=sys.stderr)
            return 2
        ok, msg = release(args[0], _flag(args, "--seat") or derive_seat(None),
                          lease=_flag(args, "--lease"), session=_env_session())
        print("helm chat: " + msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    if verb == "claims":
        rows = claims_list()
        if not rows:
            print("helm chat: no live claims")
            return 0
        for c in rows:
            print("  %s -> %s (%ds left, fence %s)" % (
                c["resource"], c["holder"], c["remaining"], c["fence"]))
        return 0
    if verb in ("verdict", "reveal"):
        print("helm chat: council is deferred to 0.3 (codex review — see the "
              "design doc §11); use the room + /premise for now", file=sys.stderr)
        return 2
    print("helm chat: unknown subcommand '%s'" % verb, file=sys.stderr)
    return 2
