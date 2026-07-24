#!/usr/bin/env bash
# NODE MIGRATION — the one-node cutover (PRD CHAT_V2, owner-approved
# 2026-07-19 "you can unify the caves without me").
#
# Migrates the team node (upstream unit dregg-cave.service, :8899 — the
# owner's attestation chain) to RAM-hot (tmpfs data-dir) with disk as
# LOG-AFTER: restore-on-boot, snapshot after every attestation turn (helm's
# cell.py fires ~/.local/bin/dregg-cave-snapshot), interval snapshot timer,
# snapshot on stop. The strict RAM canon (premise a2a-ram-only-disk-log-after)
# becomes the law for the whole node, not a chat carve-out.
#
# HARD GATES (all enforced below, the migration aborts loudly on any miss):
#   (a) full tarball backup of the live data-dir BEFORE anything
#   (b) proven restore-on-boot path installed BEFORE the flip
#   (c) premise-check MATCH before AND after + the post-flip DURABILITY CYCLE
#       (a fresh turn must survive snapshot -> stop -> tmpfs wipe -> cold boot)
#   (d) paste-ready rollback (also: --rollback), exercised by --dry-run
#   (e) meld-whisper-recv stopped around the flip, restarted + verified
#
# USAGE
#   scripts/node-migration.sh --dry-run    # rehearse: run every read-only
#                                            # gate for real, print the plan
#   scripts/node-migration.sh --execute    # the migration
#   scripts/node-migration.sh --rollback   # restore disk data-dir + old unit
#
# ENV (all optional)
#   NODE_URL         default http://127.0.0.1:8899
#   NODE_DATA        default ~/.local/share/dregg-cave/data
#   TMPFS_DIR        default /dev/shm/dregg-cave
#   NODE_PASSPHRASE  when set: after the flip, repoint helm chat at the
#                    unified node (unlock -> store url+token; retire the
#                    interim :8898 chat node). Unset: chat stays on :8898,
#                    printed as the one remaining manual step.
set -euo pipefail

NODE_URL="${NODE_URL:-http://127.0.0.1:8899}"
NODE_DATA="${NODE_DATA:-$HOME/.local/share/dregg-cave/data}"
TMPFS_DIR="${TMPFS_DIR:-/dev/shm/dregg-cave}"
UNIT=dregg-cave.service
UNIT_PATH="$HOME/.config/systemd/user/$UNIT"
BACKUP_DIR="$HOME/.local/share/dregg-cave/backups"
SNAP_BIN="$HOME/.local/bin/dregg-cave-snapshot"
RESTORE_BIN="$HOME/.local/bin/dregg-cave-restore"
TS="$(date +%Y%m%d-%H%M%S)"
PREMISE_DIR="$HOME/.helm/_global/premises"

say()  { printf '\033[1m[node-migration]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[node-migration] ABORT:\033[0m %s\n' "$*" >&2; exit 1; }

MODE="${1:-}"
[ "$MODE" = --dry-run ] || [ "$MODE" = --execute ] || [ "$MODE" = --rollback ] \
  || { grep -E '^# (NODE|USAGE|   |#)' "$0" | head -30; die "pass --dry-run, --execute or --rollback"; }
DRY=""; [ "$MODE" = --dry-run ] && DRY=1
run() { if [ -n "$DRY" ]; then say "DRY: $*"; else say "RUN: $*"; "$@"; fi; }

NODE_TOKEN=""
unlock_node() {  # a freshly-booted node serves an EMPTY api until unlocked.
                 # Captures the bearer token so every later beat authenticates
                 # with MELD_NODE_TOKEN — the unlock endpoint RATE-LIMITS (429,
                 # measured live 2026-07-19), and a passphrase-auth beat
                 # re-unlocks internally on every try, burning that budget.
  [ -n "${NODE_PASSPHRASE:-}" ] || { say "no NODE_PASSPHRASE — skipping unlock (reads may be empty on a fresh boot)"; return 0; }
  local i out tok
  for i in 1 2 3 4 5 6; do
    out="$(curl -s --max-time 5 -X POST "$NODE_URL/api/cipherclerk/unlock" \
      -H "Content-Type: application/json" -d "{\"passphrase\":\"$NODE_PASSPHRASE\"}")" || out=""
    tok="$(printf '%s' "$out" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('bearer_token') or '')
except Exception:
    print('')")"
    [ -n "$tok" ] && { NODE_TOKEN="$tok"; return 0; }
    say "unlock not accepted (attempt $i) — waiting out the rate-limit window"
    sleep 10
  done
  die "unlock never returned a bearer token at $NODE_URL — wrong passphrase or node unwell"
}

CELLS_JSON="$HOME/.helm/_global/.state/cells.json"

probe_beat() {  # mint one receipt so /api/receipts (in-memory, EMPTY each boot)
                # carries the head. Cell id + pubkey come DIRECTLY from the
                # profile json + helm's cells cache — join stdout is never
                # parsed (join on an EXISTING profile prints no cell line, so
                # the old sed parse silently skipped the faucet and the beat
                # died balance-refused). heartbeat needs --once: the bare verb
                # loops on an interval forever. Sets, from the beat's own
                # synchronous stdout JSON: PROBE_BEAT_RECEIPT,
                # PROBE_BEAT_SEQ (durable, redb-backed — the cross-boot
                # invariant), PROBE_BEAT_INDEX (per-boot, informational only),
                # PROBE_BEAT_ATTEMPTS (beat tries used; a failed try may or
                # may not have committed a turn — the gate carries that slack).
  PROBE_PROFILE="${PROBE_PROFILE:?set PROBE_PROFILE to your cell profile name}"
  PROBE_JSON="$HOME/.dregg/profiles/$PROBE_PROFILE.json"
  [ -f "$PROBE_JSON" ] || die "probe profile missing: $PROBE_JSON"
  PROBE_PUB="$(python3 -c "import json;print(json.load(open('$PROBE_JSON'))['public_key_hex'])")" \
    || die "no public_key_hex in $PROBE_JSON"
  PROBE_CELL="$(python3 -c "import json;print(json.load(open('$CELLS_JSON')).get('$PROBE_PROFILE',''))" 2>/dev/null)"
  [ -n "$PROBE_CELL" ] || die "no cell for '$PROBE_PROFILE' in $CELLS_JSON — run: MELD_NODE_URL=$NODE_URL helm cell join --profile $PROBE_PROFILE"
  curl -sf --max-time 5 -X POST "$NODE_URL/api/faucet" \
    -H "Content-Type: application/json" \
    -d "{\"recipient\":\"$PROBE_CELL\",\"amount\":10000,\"public_key\":\"$PROBE_PUB\"}" >/dev/null \
    || die "faucet refused for cell $PROBE_CELL at $NODE_URL"
  PROBE_BEAT_ATTEMPTS=0
  BEAT_LINE=""
  while [ "$PROBE_BEAT_ATTEMPTS" -lt 3 ]; do
    PROBE_BEAT_ATTEMPTS=$((PROBE_BEAT_ATTEMPTS + 1))
    if [ -n "$NODE_TOKEN" ]; then  # token auth — no per-beat unlock, no 429
      BEAT_OUT="$(MELD_NODE_URL=$NODE_URL MELD_NODE_TOKEN=$NODE_TOKEN MELD_AGENT_PROFILE=$PROBE_PROFILE \
          helm cell heartbeat --once 2>&1)" || BEAT_OUT="$BEAT_OUT"
    else
      BEAT_OUT="$(MELD_NODE_URL=$NODE_URL MELD_NODE_PASSPHRASE=${NODE_PASSPHRASE:-} MELD_AGENT_PROFILE=$PROBE_PROFILE \
          helm cell heartbeat --once 2>&1)" || BEAT_OUT="$BEAT_OUT"
    fi
    BEAT_LINE="$(printf '%s\n' "$BEAT_OUT" | python3 -c "
import json, sys
for line in reversed(sys.stdin.read().splitlines()):
    line = line.strip()
    if not line.startswith('{'):
        continue
    try:
        d = json.loads(line)
    except ValueError:
        continue
    if d.get('beat') and d.get('receipt_hash') and d.get('chain_index') is not None:
        print(json.dumps(d))
        break
")"
    [ -n "$BEAT_LINE" ] && break
    say "probe beat attempt $PROBE_BEAT_ATTEMPTS refused: $(printf '%s' "$BEAT_OUT" | tail -c 200)"
    sleep 3
  done
  [ -n "$BEAT_LINE" ] || die "probe beat minted no receipt after $PROBE_BEAT_ATTEMPTS attempts"
  PROBE_BEAT_RECEIPT="$(printf '%s' "$BEAT_LINE" | python3 -c "import json,sys;print(json.load(sys.stdin)['receipt_hash'])")"
  PROBE_BEAT_INDEX="$(printf '%s' "$BEAT_LINE" | python3 -c "import json,sys;print(json.load(sys.stdin)['chain_index'])")"
  PROBE_BEAT_SEQ="$(printf '%s' "$BEAT_LINE" | python3 -c "import json,sys;print(json.load(sys.stdin)['seq'])")"
  say "probe beat receipt $PROBE_BEAT_RECEIPT (cell $PROBE_CELL, chain_index $PROBE_BEAT_INDEX, seq $PROBE_BEAT_SEQ, attempt $PROBE_BEAT_ATTEMPTS)"
}
# FIELD SEMANTICS (measured live 2026-07-19): `seq` is the cell's DURABLE slot
# counter — it lives in the redb and survives snapshot+cold-boot; `chain_index`
# is the per-boot in-memory receipt chain and RESETS every restart (dregg#62
# class) — it must never be used as a cross-restart invariant. attested_height
# batches turns per attestation, lands async (~5s), and on the pre-migration
# DISK-mode node RESTARTS at the last durable checkpoint: that node held recent
# turns in RAM only (measured: dregg.redb untouched across 17min of committed
# turns AND a clean SIGTERM stop, while every async proof panicked with
# "committed-but-unattested"). The tmpfs-mode node DOES write the redb per
# turn — which is the durability hole this migration exists to close.

head_json() {  # RESTART-STABLE invariant: attested_height survives a reboot;
               # the receipts list and per-turn lookups do NOT (dregg#62 class).
               # A fresh beat's height lands asynchronously (~5s) — poll until
               # the head receipt carries it, {} only after a real timeout.
  python3 - "$NODE_URL" <<'PY'
import json, sys, time, urllib.request
url = sys.argv[1]
for _ in range(24):
    try:
        r = json.load(urllib.request.urlopen(url + "/api/receipts", timeout=4))
        h = r[0].get("attested_height") if r else None
    except Exception:
        h = None
    if h is not None:
        print(json.dumps({"attested_height": h}))
        break
    time.sleep(2.5)
else:
    print("{}")
PY
}

height_of() { printf '%s' "$1" | python3 -c "import json,sys;print(json.load(sys.stdin).get('attested_height',-1))"; }

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
  rm -rf "$NODE_DATA"; mkdir -p "$(dirname "$NODE_DATA")"
  tar xzf "$LAST_TAR" -C "$(dirname "$NODE_DATA")"
  systemctl --user disable --now dregg-cave-snapshot.timer 2>/dev/null || true
  systemctl --user daemon-reload
  systemctl --user start "$UNIT"
  sleep 3; curl -sf --max-time 5 "$NODE_URL/api/receipts" >/dev/null || die "node not answering after rollback"
  systemctl --user restart meld-whisper-recv.service 2>/dev/null || true
  say "rollback complete — verify: helm cell status && helm premise-check <id>"
  say "NOTE: rollback restores the last durable checkpoint; turns the old"
  say "      process held only in RAM are not recoverable by ANY path."
  exit 0
fi

# ── PRE-FLIGHT (both --dry-run and --execute run these gates; the only write
# is the probe's faucet+beat pair — append-only turns, no state mutated) ─────
command -v helm >/dev/null || die "helm not on PATH"
[ -f "$UNIT_PATH" ] || die "unit not found: $UNIT_PATH"
[ -d "$NODE_DATA" ] || die "node data-dir not found: $NODE_DATA"
mkdir -p "$BACKUP_DIR"
[ -w "$BACKUP_DIR" ] || die "backup dir not writable: $BACKUP_DIR"
curl -sf --max-time 4 "$NODE_URL/api/receipts" >/dev/null \
  || die "node not answering at $NODE_URL — the migration wants a LIVE chain to compare against"
unlock_node
probe_beat   # both sides probe: /api/receipts is in-memory, empty each boot
BEAT_SEQ_BEFORE="$PROBE_BEAT_SEQ"
HEAD_BEFORE="$(head_json)"
[ "$HEAD_BEFORE" != "{}" ] || die "no attested_height even after a probe beat — refusing to migrate blind"
say "chain head BEFORE: $HEAD_BEFORE, probe-cell seq $BEAT_SEQ_BEFORE"
say "NOTE: the disk-mode node persists only through its last durable checkpoint —"
say "      turns newer than that live in its RAM and CANNOT survive any stop"
say "      (no flush path exists; rollback restores the same checkpoint). The"
say "      restored head will read lower; the premise gate is the content arbiter."
premise_gate
WHISPER_WAS_ACTIVE=""
systemctl --user is-active meld-whisper-recv.service >/dev/null 2>&1 && WHISPER_WAS_ACTIVE=1
say "meld-whisper-recv active: ${WHISPER_WAS_ACTIVE:-no}"
say "backup will be: $BACKUP_DIR/dregg-cave-data-$TS.tar.gz"
DATA_MB="$(du -sm "$NODE_DATA" | cut -f1)"
say "data-dir size: ${DATA_MB}MB (tmpfs target: $TMPFS_DIR)"
FREE_MB="$(df -m /dev/shm | awk 'NR==2{print $4}')"
[ "$FREE_MB" -gt $((DATA_MB * 2 + 64)) ] || die "/dev/shm too small: ${FREE_MB}MB free for ${DATA_MB}MB node"

# the paste-ready rollback block — gate (d); printed in BOTH modes
cat <<EOF
── ROLLBACK (paste-ready; also: $0 --rollback) ─────────────────────────────
  systemctl --user stop $UNIT
  cp $UNIT_PATH.pre-unification.bak $UNIT_PATH
  rm -rf $NODE_DATA && tar xzf $BACKUP_DIR/dregg-cave-data-$TS.tar.gz -C $(dirname "$NODE_DATA")
  systemctl --user disable --now dregg-cave-snapshot.timer; systemctl --user daemon-reload
  systemctl --user start $UNIT && sleep 3 && curl -s $NODE_URL/api/receipts | head -c 120
  systemctl --user restart meld-whisper-recv.service
────────────────────────────────────────────────────────────────────────────
EOF

if [ -n "$DRY" ]; then
  say "DRY: (e) stop meld-whisper-recv + $UNIT"
  say "DRY: (a) tar czf $BACKUP_DIR/dregg-cave-data-$TS.tar.gz $(basename "$NODE_DATA") + sha256"
  say "DRY: (b) install $SNAP_BIN + $RESTORE_BIN + snapshot timer (5min) + unit rewrite:"
  say "DRY:     ExecStartPre=$RESTORE_BIN   (restore-on-boot: tmpfs empty -> copy snapshot)"
  say "DRY:     ExecStart --data-dir $TMPFS_DIR   (RAM-hot)"
  say "DRY:     ExecStopPost=$SNAP_BIN   (flush on stop)"
  say "DRY:     + helm cell.py fires $SNAP_BIN after every attestation turn"
  say "DRY: rsync -a $NODE_DATA/ $TMPFS_DIR/ ; daemon-reload ; start ; wait health"
  say "DRY: (c) probe the restored head (RAM-delta reads lower — disk-mode hole) + premise gate"
  say "DRY: (c2) durability cycle: beat -> snapshot -> stop -> wipe tmpfs -> restore -> boot -> beat (seq must continue)"
  say "DRY: (e) restart meld-whisper-recv + verify active"
  say "DRY: repoint helm chat at $NODE_URL (NODE_PASSPHRASE $([ -n "${NODE_PASSPHRASE:-}" ] && printf set || printf 'UNSET -> manual step')) + retire the interim chat node"
  say "DRY RUN COMPLETE — every gate passed (probe minted its append-only faucet+beat turns). Execute with: $0 --execute"
  exit 0
fi

# ── EXECUTE ─────────────────────────────────────────────────────────────────
say "(e) stopping meld-whisper-recv + the node"
systemctl --user stop meld-whisper-recv.service 2>/dev/null || true
systemctl --user stop "$UNIT"

say "(a) full tarball backup"
tar czf "$BACKUP_DIR/dregg-cave-data-$TS.tar.gz" -C "$(dirname "$NODE_DATA")" "$(basename "$NODE_DATA")"
sha256sum "$BACKUP_DIR/dregg-cave-data-$TS.tar.gz" | tee "$BACKUP_DIR/dregg-cave-data-$TS.sha256"
tar tzf "$BACKUP_DIR/dregg-cave-data-$TS.tar.gz" >/dev/null || die "backup tarball unreadable"

say "(b) installing snapshot + restore helpers"
mkdir -p "$(dirname "$SNAP_BIN")"
cat > "$SNAP_BIN" <<EOF
#!/usr/bin/env bash
# dregg node LOG-AFTER flush: RAM data-dir -> disk snapshot (atomic-enough: rsync
# then sync). Installed by scripts/node-migration.sh; fired by the timer, on unit stop,
# and by helm after every attestation turn.
set -eu
[ -d "$TMPFS_DIR" ] && [ -e "$TMPFS_DIR/node.key" ] || exit 0
rsync -a --delete "$TMPFS_DIR/" "$NODE_DATA/"
sync
EOF
cat > "$RESTORE_BIN" <<EOF
#!/usr/bin/env bash
# dregg node restore-on-boot: tmpfs died (reboot) -> repopulate from the disk
# snapshot. NEVER clobbers a live tmpfs (guards on node.key presence).
set -eu
mkdir -p "$TMPFS_DIR"
if [ ! -e "$TMPFS_DIR/node.key" ] && [ -e "$NODE_DATA/node.key" ]; then
  rsync -a "$NODE_DATA/" "$TMPFS_DIR/"
fi
EOF
chmod 755 "$SNAP_BIN" "$RESTORE_BIN"

say "(b) proving the restore path BEFORE the flip"
rm -rf "$TMPFS_DIR"
"$RESTORE_BIN"
[ -e "$TMPFS_DIR/node.key" ] || die "restore-on-boot helper failed to repopulate tmpfs"
diff <(cd "$NODE_DATA" && find . -type f | sort) <(cd "$TMPFS_DIR" && find . -type f | sort) \
  || die "restore produced a different file set"
cmp -s "$NODE_DATA/dregg.redb" "$TMPFS_DIR/dregg.redb" \
  || die "restore produced different redb bytes"

say "rewriting the unit (backup: $UNIT_PATH.pre-unification.bak)"
cp "$UNIT_PATH" "$UNIT_PATH.pre-unification.bak"
python3 - "$UNIT_PATH" "$NODE_DATA" "$TMPFS_DIR" "$RESTORE_BIN" "$SNAP_BIN" <<'PYEOF'
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
Description=dregg node log-after snapshot (tmpfs -> disk)
[Service]
Type=oneshot
ExecStart=$SNAP_BIN
EOF
cat > "$HOME/.config/systemd/user/dregg-cave-snapshot.timer" <<EOF
[Unit]
Description=dregg node log-after snapshot interval
[Timer]
OnBootSec=5min
OnUnitActiveSec=5min
[Install]
WantedBy=timers.target
EOF

say "starting the RAM-hot node"
systemctl --user daemon-reload
systemctl --user enable --now dregg-cave-snapshot.timer
systemctl --user start "$UNIT"
for i in $(seq 1 40); do
  curl -sf --max-time 2 "$NODE_URL/api/receipts" >/dev/null && break
  sleep 0.5
  [ "$i" -lt 40 ] || die "node never answered after the flip — rollback block above"
done

say "(c) carried state + premise gate + the durability cycle"
unlock_node
probe_beat
HEAD_AFTER="$(head_json)"
[ "$HEAD_AFTER" != "{}" ] || die "restored node minted a beat but never surfaced attested_height — rollback NOW (block above)"
H_BEFORE=$(height_of "$HEAD_BEFORE")
H_AFTER=$(height_of "$HEAD_AFTER")
say "chain head AFTER the flip: $HEAD_AFTER (BEFORE read $HEAD_BEFORE)"
say "RAM-delta lost across the flip: $((H_BEFORE - H_AFTER)) attestations of probe/presence noise"
say "(the disk-mode node's known hole — see the field-semantics note in this script;"
say " content-carrying state is arbitrated by the premise gate, next)"
premise_gate

# (c2) THE DURABILITY CYCLE — the hard gate this migration exists to pass, a
# property the disk-mode node measurably LACKED: a fresh turn must survive
# beat -> snapshot -> stop -> tmpfs wipe -> restore-from-disk -> boot. `seq`
# is the durable per-cell counter (redb-backed): the post-cycle beat must
# continue the pre-cycle seq, and attested height must not regress.
say "(c2) durability cycle: snapshot -> stop -> wipe tmpfs -> restore -> boot -> beat"
SEQ_CYCLE_START="$PROBE_BEAT_SEQ"
"$SNAP_BIN"
systemctl --user stop "$UNIT"
rm -rf "$TMPFS_DIR"
systemctl --user start "$UNIT"
for i in $(seq 1 40); do
  curl -sf --max-time 2 "$NODE_URL/api/receipts" >/dev/null && break
  sleep 0.5
  [ "$i" -lt 40 ] || die "node never answered after the durability-cycle boot — rollback NOW (block above)"
done
unlock_node
probe_beat
SEQ_FLOOR=$((SEQ_CYCLE_START + 1))
SEQ_CAP=$((SEQ_CYCLE_START + PROBE_BEAT_ATTEMPTS))
[ "$PROBE_BEAT_SEQ" -ge "$SEQ_FLOOR" ] && [ "$PROBE_BEAT_SEQ" -le "$SEQ_CAP" ] \
  || die "DURABILITY CYCLE FAILED: probe seq $SEQ_CYCLE_START -> $PROBE_BEAT_SEQ (expected $SEQ_FLOOR..$SEQ_CAP) — new turns are NOT surviving the snapshot round-trip; rollback NOW (block above)"
HEAD_CYCLE="$(head_json)"
H_CYCLE=$(height_of "$HEAD_CYCLE")
[ "$H_CYCLE" -ge "$H_AFTER" ] \
  || die "DURABILITY CYCLE FAILED: attested height regressed $H_AFTER -> $H_CYCLE — rollback NOW (block above)"
say "durability cycle PASSED: seq $SEQ_CYCLE_START -> $PROBE_BEAT_SEQ, height $H_AFTER -> $H_CYCLE across a cold boot from the disk snapshot"

say "(e) restarting meld-whisper-recv"
if [ -n "$WHISPER_WAS_ACTIVE" ]; then
  systemctl --user start meld-whisper-recv.service
  sleep 2
  systemctl --user is-active meld-whisper-recv.service >/dev/null || die "meld-whisper-recv did not come back"
  say "meld-whisper-recv reconnected"
fi

say "first log-after snapshot"
"$SNAP_BIN"

if [ -n "${NODE_PASSPHRASE:-}" ]; then
  say "repointing helm chat at the unified node"
  helm chat log-flush || true
  python3 - "$NODE_URL" <<'PYEOF'
import os, sys, time
from helm import chatnode
url = sys.argv[1]
err = None
for i in range(6):  # the unlock endpoint rate-limits (429) — wait it out
    st, err = chatnode.provision(url, os.environ["NODE_PASSPHRASE"])
    if not err:
        print("chat node state ->", url)
        break
    print("provision attempt %d: %s" % (i + 1, err), file=sys.stderr)
    time.sleep(15)
else:
    raise SystemExit("chat repoint failed after retries: " + err)
PYEOF
  systemctl --user disable --now helm-chat-node.service 2>/dev/null || true
  systemctl --user disable --now helm-chat-cave.service 2>/dev/null || true  # pre-rename unit name
  say "interim chat node (:8898) retired; chat rides the one node"
else
  say "NODE_PASSPHRASE unset — chat stays on the interim :8898 node."
  say "manual step: NODE_PASSPHRASE=... python3 -c 'from helm import chatnode; print(chatnode.provision(\"$NODE_URL\"))'"
fi

say "MIGRATION COMPLETE. The node is RAM-hot with disk as log-after."
say "verify: helm cell status ; helm doctor ; helm premise-check <id> ; helm chat node status"
