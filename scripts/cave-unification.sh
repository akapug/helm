#!/usr/bin/env bash
# CAVE UNIFICATION — the ONE-CAVE ceremony (PRD CHAT_V2, owner-approved
# 2026-07-19 "you can unify the caves without me").
#
# Migrates the team cave (dregg-cave.service, :8899 — the owner's attestation
# chain) to RAM-hot (tmpfs data-dir) with disk as LOG-AFTER: restore-on-boot,
# snapshot after every attestation turn (helm's cell.py fires
# ~/.local/bin/dregg-cave-snapshot), interval snapshot timer, snapshot on
# stop. The strict RAM canon (premise a2a-ram-only-disk-log-after) becomes
# the universal cave law, not a chat carve-out.
#
# HARD GATES (all enforced below, ceremony aborts loudly on any miss):
#   (a) full tarball backup of the live data-dir BEFORE anything
#   (b) proven restore-on-boot path installed BEFORE the flip
#   (c) premise-check MATCH + chain-head/receipt comparison before AND after
#   (d) paste-ready rollback (also: --rollback), exercised by --dry-run
#   (e) meld-whisper-recv stopped around the flip, restarted + verified
#
# USAGE
#   scripts/cave-unification.sh --dry-run    # rehearse: run every read-only
#                                            # gate for real, print the plan
#   scripts/cave-unification.sh --execute    # the ceremony
#   scripts/cave-unification.sh --rollback   # restore disk data-dir + old unit
#
# ENV (all optional)
#   CAVE_URL         default http://127.0.0.1:8899
#   CAVE_DATA        default ~/.local/share/dregg-cave/data
#   TMPFS_DIR        default /dev/shm/dregg-cave
#   CAVE_PASSPHRASE  when set: after the flip, repoint helm chat at the
#                    unified cave (unlock -> store url+token; retire the
#                    interim :8898 chat node). Unset: chat stays on :8898,
#                    printed as the one remaining manual step.
set -euo pipefail

CAVE_URL="${CAVE_URL:-http://127.0.0.1:8899}"
CAVE_DATA="${CAVE_DATA:-$HOME/.local/share/dregg-cave/data}"
TMPFS_DIR="${TMPFS_DIR:-/dev/shm/dregg-cave}"
UNIT=dregg-cave.service
UNIT_PATH="$HOME/.config/systemd/user/$UNIT"
BACKUP_DIR="$HOME/.local/share/dregg-cave/backups"
SNAP_BIN="$HOME/.local/bin/dregg-cave-snapshot"
RESTORE_BIN="$HOME/.local/bin/dregg-cave-restore"
TS="$(date +%Y%m%d-%H%M%S)"
PREMISE_DIR="$HOME/.helm/_global/premises"

say()  { printf '\033[1m[cave-unification]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[cave-unification] ABORT:\033[0m %s\n' "$*" >&2; exit 1; }

MODE="${1:-}"
[ "$MODE" = --dry-run ] || [ "$MODE" = --execute ] || [ "$MODE" = --rollback ] \
  || { grep -E '^# (CAVE|USAGE|   |#)' "$0" | head -30; die "pass --dry-run, --execute or --rollback"; }
DRY=""; [ "$MODE" = --dry-run ] && DRY=1
run() { if [ -n "$DRY" ]; then say "DRY: $*"; else say "RUN: $*"; "$@"; fi; }

head_json() { curl -sf --max-time 4 "$CAVE_URL/api/receipts" | python3 -c '
import json,sys
r=json.load(sys.stdin)
print(json.dumps({"chain_index":r[0]["chain_index"],"receipt_hash":r[0]["receipt_hash"]}) if r else "{}")'; }

premise_gate() {  # gate (c): every attested premise must re-verify MATCH
  local fails=0 n=0 id
  for f in "$PREMISE_DIR"/prior-*.md; do
    [ -e "$f" ] || continue
    grep -q '^  attest_turn:' "$f" || continue
    id="$(sed -n 's/^  id: //p' "$f" | head -1)"
    [ -n "$id" ] || continue
    n=$((n+1))
    if helm premise-check "$id" >/dev/null 2>&1; then
      say "premise-check MATCH: $id"
    else
      say "premise-check FAILED: $id"; fails=$((fails+1))
    fi
  done
  [ "$n" -gt 0 ] || say "WARNING: no attested premises found under $PREMISE_DIR"
  [ "$fails" -eq 0 ] || die "premise-check gate: $fails of $n attested premises failed"
  say "premise gate: $n attested premises MATCH"
}

# ── ROLLBACK ────────────────────────────────────────────────────────────────
if [ "$MODE" = --rollback ]; then
  LAST_TAR="$(ls -t "$BACKUP_DIR"/dregg-cave-data-*.tar.gz 2>/dev/null | head -1)"
  [ -n "$LAST_TAR" ] || die "no backup tarball in $BACKUP_DIR"
  say "rolling back from $LAST_TAR"
  systemctl --user stop "$UNIT" || true
  [ -f "$UNIT_PATH.pre-unification.bak" ] && cp "$UNIT_PATH.pre-unification.bak" "$UNIT_PATH"
  rm -rf "$CAVE_DATA"; mkdir -p "$(dirname "$CAVE_DATA")"
  tar xzf "$LAST_TAR" -C "$(dirname "$CAVE_DATA")"
  systemctl --user disable --now dregg-cave-snapshot.timer 2>/dev/null || true
  systemctl --user daemon-reload
  systemctl --user start "$UNIT"
  sleep 3; curl -sf --max-time 5 "$CAVE_URL/api/receipts" >/dev/null || die "cave not answering after rollback"
  systemctl --user restart meld-whisper-recv.service 2>/dev/null || true
  say "rollback complete — verify: helm cell status && helm premise-check <id>"
  exit 0
fi

# ── PRE-FLIGHT (both --dry-run and --execute run these READ-ONLY gates) ─────
command -v helm >/dev/null || die "helm not on PATH"
[ -f "$UNIT_PATH" ] || die "unit not found: $UNIT_PATH"
[ -d "$CAVE_DATA" ] || die "cave data-dir not found: $CAVE_DATA"
mkdir -p "$BACKUP_DIR"
[ -w "$BACKUP_DIR" ] || die "backup dir not writable: $BACKUP_DIR"
HEAD_BEFORE="$(head_json)" || die "cave not answering at $CAVE_URL — ceremony wants a LIVE chain to compare against"
[ "$HEAD_BEFORE" != "{}" ] || die "cave has no receipts — nothing to migrate safely"
say "chain head BEFORE: $HEAD_BEFORE"
premise_gate
WHISPER_WAS_ACTIVE=""
systemctl --user is-active meld-whisper-recv.service >/dev/null 2>&1 && WHISPER_WAS_ACTIVE=1
say "meld-whisper-recv active: ${WHISPER_WAS_ACTIVE:-no}"
say "backup will be: $BACKUP_DIR/dregg-cave-data-$TS.tar.gz"
DATA_MB="$(du -sm "$CAVE_DATA" | cut -f1)"
say "data-dir size: ${DATA_MB}MB (tmpfs target: $TMPFS_DIR)"
FREE_MB="$(df -m /dev/shm | awk 'NR==2{print $4}')"
[ "$FREE_MB" -gt $((DATA_MB * 2 + 64)) ] || die "/dev/shm too small: ${FREE_MB}MB free for ${DATA_MB}MB cave"

# the paste-ready rollback block — gate (d); printed in BOTH modes
cat <<EOF
── ROLLBACK (paste-ready; also: $0 --rollback) ─────────────────────────────
  systemctl --user stop $UNIT
  cp $UNIT_PATH.pre-unification.bak $UNIT_PATH
  rm -rf $CAVE_DATA && tar xzf $BACKUP_DIR/dregg-cave-data-$TS.tar.gz -C $(dirname "$CAVE_DATA")
  systemctl --user disable --now dregg-cave-snapshot.timer; systemctl --user daemon-reload
  systemctl --user start $UNIT && sleep 3 && curl -s $CAVE_URL/api/receipts | head -c 120
  systemctl --user restart meld-whisper-recv.service
────────────────────────────────────────────────────────────────────────────
EOF

if [ -n "$DRY" ]; then
  say "DRY: (e) stop meld-whisper-recv + $UNIT"
  say "DRY: (a) tar czf $BACKUP_DIR/dregg-cave-data-$TS.tar.gz $(basename "$CAVE_DATA") + sha256"
  say "DRY: (b) install $SNAP_BIN + $RESTORE_BIN + snapshot timer (5min) + unit rewrite:"
  say "DRY:     ExecStartPre=$RESTORE_BIN   (restore-on-boot: tmpfs empty -> copy snapshot)"
  say "DRY:     ExecStart --data-dir $TMPFS_DIR   (RAM-hot)"
  say "DRY:     ExecStopPost=$SNAP_BIN   (flush on stop)"
  say "DRY:     + helm cell.py fires $SNAP_BIN after every attestation turn"
  say "DRY: rsync -a $CAVE_DATA/ $TMPFS_DIR/ ; daemon-reload ; start ; wait health"
  say "DRY: (c) compare chain head + re-run the premise gate"
  say "DRY: (e) restart meld-whisper-recv + verify active"
  say "DRY: repoint helm chat at $CAVE_URL (CAVE_PASSPHRASE ${CAVE_PASSPHRASE:+set}${CAVE_PASSPHRASE:-UNSET -> manual step}) + retire helm-chat-cave"
  say "DRY RUN COMPLETE — all read-only gates passed. Execute with: $0 --execute"
  exit 0
fi

# ── EXECUTE ─────────────────────────────────────────────────────────────────
say "(e) stopping meld-whisper-recv + the cave"
systemctl --user stop meld-whisper-recv.service 2>/dev/null || true
systemctl --user stop "$UNIT"

say "(a) full tarball backup"
tar czf "$BACKUP_DIR/dregg-cave-data-$TS.tar.gz" -C "$(dirname "$CAVE_DATA")" "$(basename "$CAVE_DATA")"
sha256sum "$BACKUP_DIR/dregg-cave-data-$TS.tar.gz" | tee "$BACKUP_DIR/dregg-cave-data-$TS.sha256"
tar tzf "$BACKUP_DIR/dregg-cave-data-$TS.tar.gz" >/dev/null || die "backup tarball unreadable"

say "(b) installing snapshot + restore helpers"
mkdir -p "$(dirname "$SNAP_BIN")"
cat > "$SNAP_BIN" <<EOF
#!/usr/bin/env bash
# dregg-cave LOG-AFTER flush: RAM cave -> disk snapshot (atomic-enough: rsync
# then sync). Installed by cave-unification; fired by the timer, on unit stop,
# and by helm after every attestation turn.
set -eu
[ -d "$TMPFS_DIR" ] && [ -e "$TMPFS_DIR/node.key" ] || exit 0
rsync -a --delete "$TMPFS_DIR/" "$CAVE_DATA/"
sync
EOF
cat > "$RESTORE_BIN" <<EOF
#!/usr/bin/env bash
# dregg-cave restore-on-boot: tmpfs died (reboot) -> repopulate from the disk
# snapshot. NEVER clobbers a live tmpfs (guards on node.key presence).
set -eu
mkdir -p "$TMPFS_DIR"
if [ ! -e "$TMPFS_DIR/node.key" ] && [ -e "$CAVE_DATA/node.key" ]; then
  rsync -a "$CAVE_DATA/" "$TMPFS_DIR/"
fi
EOF
chmod 755 "$SNAP_BIN" "$RESTORE_BIN"

say "(b) proving the restore path BEFORE the flip"
rm -rf "$TMPFS_DIR"
"$RESTORE_BIN"
[ -e "$TMPFS_DIR/node.key" ] || die "restore-on-boot helper failed to repopulate tmpfs"
diff <(cd "$CAVE_DATA" && find . -type f | sort) <(cd "$TMPFS_DIR" && find . -type f | sort) \
  || die "restore produced a different file set"

say "rewriting the unit (backup: $UNIT_PATH.pre-unification.bak)"
cp "$UNIT_PATH" "$UNIT_PATH.pre-unification.bak"
python3 - "$UNIT_PATH" "$CAVE_DATA" "$TMPFS_DIR" "$RESTORE_BIN" "$SNAP_BIN" <<'PYEOF'
import sys
path, disk, tmpfs, restore, snap = sys.argv[1:]
lines = open(path).read().splitlines()
out = []
for l in lines:
    if l.startswith("ExecStart="):
        out.append("ExecStartPre=" + restore)
        out.append(l.replace("--data-dir " + disk, "--data-dir " + tmpfs))
        out.append("ExecStopPost=" + snap)
    elif l.startswith(("ExecStartPre=", "ExecStopPost=")):
        continue
    else:
        out.append(l)
body = "\n".join(out) + "\n"
assert tmpfs in body, "data-dir rewrite failed — check the unit's ExecStart"
open(path, "w").write(body)
PYEOF

say "(b) installing the interval snapshot timer (5min)"
cat > "$HOME/.config/systemd/user/dregg-cave-snapshot.service" <<EOF
[Unit]
Description=dregg cave log-after snapshot (tmpfs -> disk)
[Service]
Type=oneshot
ExecStart=$SNAP_BIN
EOF
cat > "$HOME/.config/systemd/user/dregg-cave-snapshot.timer" <<EOF
[Unit]
Description=dregg cave log-after snapshot interval
[Timer]
OnBootSec=5min
OnUnitActiveSec=5min
[Install]
WantedBy=timers.target
EOF

say "starting the RAM-hot cave"
systemctl --user daemon-reload
systemctl --user enable --now dregg-cave-snapshot.timer
systemctl --user start "$UNIT"
for i in $(seq 1 40); do
  curl -sf --max-time 2 "$CAVE_URL/api/receipts" >/dev/null && break
  sleep 0.5
  [ "$i" -lt 40 ] || die "cave never answered after the flip — rollback block above"
done

say "(c) comparing the chain head + re-running the premise gate"
HEAD_AFTER="$(head_json)"
say "chain head AFTER:  $HEAD_AFTER"
[ "$HEAD_BEFORE" = "$HEAD_AFTER" ] || die "CHAIN HEAD CHANGED across the flip — rollback NOW (block above)"
premise_gate

say "(e) restarting meld-whisper-recv"
if [ -n "$WHISPER_WAS_ACTIVE" ]; then
  systemctl --user start meld-whisper-recv.service
  sleep 2
  systemctl --user is-active meld-whisper-recv.service >/dev/null || die "meld-whisper-recv did not come back"
  say "meld-whisper-recv reconnected"
fi

say "first log-after snapshot"
"$SNAP_BIN"

if [ -n "${CAVE_PASSPHRASE:-}" ]; then
  say "repointing helm chat at the unified cave"
  helm chat log-flush || true
  python3 - "$CAVE_URL" <<'PYEOF'
import sys
from helm import chatnode
url = sys.argv[1]
st, err = chatnode.provision(url, __import__("os").environ["CAVE_PASSPHRASE"])
if err:
    raise SystemExit("chat repoint failed: " + err)
print("chat node state ->", url)
PYEOF
  systemctl --user disable --now helm-chat-cave.service 2>/dev/null || true
  say "interim chat cave (:8898) retired; chat rides the ONE cave"
else
  say "CAVE_PASSPHRASE unset — chat stays on the interim :8898 node."
  say "manual step: CAVE_PASSPHRASE=... python3 -c 'from helm import chatnode; print(chatnode.provision(\"$CAVE_URL\"))'"
fi

say "UNIFICATION COMPLETE. The one cave is RAM-hot with disk as log-after."
say "verify: helm cell status ; helm doctor ; helm premise-check <id> ; helm chat node status"
