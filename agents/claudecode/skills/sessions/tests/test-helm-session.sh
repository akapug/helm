#!/usr/bin/env bash
# Deterministic guard tests for scripts/helm-session.sh. The load-bearing safety invariant: a copy/convert
# runs as a DRY-RUN (cv gets `--out`) unless --apply is passed. Tested with a fake cv (no real cv/systemd
# needed → CI-portable). Discovered via agents/**/tests/test-*.sh.
set -uo pipefail

REPO="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel 2>/dev/null || echo "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../../.." && pwd)")"
SUT="$REPO/scripts/helm-session.sh"
fails=0
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

# a fake cv: logs its args, fakes show (stable cwd) + port/convert (new id)
STUB="$tmp/cv"; cat > "$STUB" <<STUBEOF
#!/usr/bin/env bash
echo "\$@" >> "$tmp/cvlog"
case "\$1" in
  show) echo '{"cwd":"/orig/cwd"}' ;;
  port|convert) echo "wrote /tmp/x/deadbeef.jsonl (deadbeef)" ;;
  export) echo "# exported" ;;
esac
STUBEOF
chmod +x "$STUB"

run() { : > "$tmp/cvlog"; HELM_SESSION_CV_BIN="$STUB" HELM_SESSION_SCRATCH="$tmp/scratch" "$SUT" "$@" >"$tmp/out" 2>"$tmp/err"; echo $?; }
fail() { echo "FAIL: $1"; fails=$((fails+1)); }

# 1. dry-run (no --apply): cv port MUST get --out (real storage untouched)
rc="$(run rehome sess123 --to-dir /new/cwd)"
[ "$rc" = 0 ] || fail "dry-run rehome should exit 0 (got $rc)"
grep -q '^port sess123 .*--out' "$tmp/cvlog" || fail "dry-run must pass --out to cv port (rail)"
grep -qi 'DRY-RUN' "$tmp/err" || fail "dry-run must announce DRY-RUN"

# 2. --apply: cv port MUST NOT get --out (writes for real), then verifies
rc="$(run rehome sess123 --to-dir /new/cwd --apply)"
[ "$rc" = 0 ] || fail "apply rehome should exit 0 (got $rc)"
if grep -q '^port sess123 .*--out' "$tmp/cvlog"; then fail "apply must NOT pass --out (real write)"; fi
grep -qi 'original .* intact' "$tmp/err" || fail "apply must assert original-untouched"

# 3. port-harness (convert) dry-run also uses --out
rc="$(run port-harness sess123 --to codex)"
grep -q '^convert sess123 .*--out' "$tmp/cvlog" || fail "port-harness dry-run must pass --out"

# 4. arg validation (no cv needed)
val() { HELM_SESSION_CV_BIN="$STUB" "$SUT" "$@" >/dev/null 2>&1; echo $?; }
[ "$(val)" = 2 ]                              || fail "no args -> exit 2"
[ "$(val bogus sess)" = 2 ]                   || fail "unknown cmd -> exit 2"
[ "$(val rehome)" = 2 ]                       || fail "rehome missing id -> exit 2"
[ "$(val rehome sess)" = 2 ]                  || fail "rehome missing --to-dir -> exit 2"
[ "$(val port-harness sess)" = 2 ]           || fail "port-harness missing --to -> exit 2"
[ "$(val export sess --format pdf)" = 2 ]    || fail "bad export format -> exit 2"
[ "$(val --help)" = 0 ]                       || fail "help -> exit 0"

# 5. missing source session fails loud (stub returns empty show for a 'missing' id)
STUB2="$tmp/cv2"; cat > "$STUB2" <<'S2'
#!/usr/bin/env bash
case "$1" in show) exit 0;; esac
S2
chmod +x "$STUB2"
HELM_SESSION_CV_BIN="$STUB2" HELM_SESSION_SCRATCH="$tmp/s" "$SUT" rehome ghost --to-dir /x >/dev/null 2>"$tmp/err2"; rc=$?
[ "$rc" = 4 ] || fail "missing source session -> exit 4 (got $rc)"

# 6. original-untouched rail is NON-VACUOUS: cv show returns JSON but no cwd -> --apply FAILS LOUD (exit 7),
#    never lets the ""=="" comparison silently no-op the rail.
STUB3="$tmp/cv3"; cat > "$STUB3" <<'S3'
#!/usr/bin/env bash
case "$1" in
  show) echo '{"messages":[]}' ;;                       # session resolves, but NO cwd field
  port|convert) echo "wrote /tmp/x/deadbeef.jsonl (deadbeef)" ;;
esac
S3
chmod +x "$STUB3"
HELM_SESSION_CV_BIN="$STUB3" HELM_SESSION_SCRATCH="$tmp/s3" "$SUT" rehome nocwd --to-dir /x --apply >/dev/null 2>"$tmp/err3"; rc=$?
[ "$rc" = 7 ] || fail "unreadable source cwd on --apply -> exit 7, not vacuous pass (got $rc)"
grep -qi 'cannot read source .* cwd' "$tmp/err3" || fail "unreadable cwd must name the untouched-rail refusal"

# --- resurrect: per-expert channel isolation (a real git repo + linked worktree; git is a hard dep) ---
GITROOT="$tmp/repo"; mkdir -p "$GITROOT"
git -C "$GITROOT" init -q
git -C "$GITROOT" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
WTREE="$tmp/wt-expert"; git -C "$GITROOT" worktree add -q "$WTREE" -b expert-home

# resurrect that REACHES the port needs a slug-aware cv stub (keys the ported transcript into the --to-dir
# slug dir, like real cv port) + a hermetic projects root for the native-memory re-key verify. rrun() uses it.
RSTUB="$tmp/cvr"; cat > "$RSTUB" <<'RS'
#!/usr/bin/env bash
verb="$1"; shift
echo "$verb $*" >> "$HELM_SESSION_CVLOG"
todir=""; out=""
while [ $# -gt 0 ]; do case "$1" in --to-dir) todir="$2"; shift 2;; --out) out="$2"; shift 2;; *) shift;; esac; done
case "$verb" in
  show) echo '{"cwd":"/orig/cwd"}' ;;
  port)
    slug="$(printf '%s' "$todir" | sed 's#[/.]#-#g')"
    root="${out:-$HELM_SESSION_PROJECTS_DIR}"
    mkdir -p "$root/$slug"; : > "$root/$slug/deadbeef.jsonl"
    echo "wrote $root/$slug/deadbeef.jsonl (deadbeef)" ;;
esac
RS
chmod +x "$RSTUB"
PROJ="$tmp/projects"; mkdir -p "$PROJ"
rrun() { : > "$tmp/cvlog"; HELM_SESSION_CV_BIN="$RSTUB" HELM_SESSION_CVLOG="$tmp/cvlog" HELM_SESSION_SCRATCH="$tmp/rscratch" HELM_SESSION_PROJECTS_DIR="$PROJ" "$SUT" "$@" >"$tmp/out" 2>"$tmp/err"; echo $?; }

# 7. resurrect REFUSES a non-worktree PROJECT_DIR (exit 8): the shared/main checkout, a plain dir, a missing dir.
rc="$(run resurrect sess123 --project-dir "$GITROOT" --apply)"
[ "$rc" = 8 ] || fail "resurrect into the SHARED/main checkout -> exit 8 (got $rc)"
grep -qi 'not a dedicated worktree\|shared/main checkout' "$tmp/err" || fail "exit-8 must name the shared-checkout refusal"
mkdir -p "$tmp/plain"
[ "$(run resurrect sess123 --project-dir "$tmp/plain" --apply)" = 8 ] || fail "resurrect into a non-git dir -> exit 8"
[ "$(run resurrect sess123 --project-dir "$tmp/nope" --apply)" = 8 ] || fail "resurrect into a missing dir -> exit 8"

# 7b. exit-8 diagnostics steer to the HELM lifecycle (`helm work claim`), never raw `git worktree add` —
#     these refusals are exactly where a user lands when the guard fires, so docs and diagnostics must agree
#     (sessions/SKILL.md: mint rooms via `helm work claim <lane>`, lease + guard rails).
grep -q 'helm work claim' "$tmp/err" || fail "missing-dir refusal must steer to 'helm work claim'"
run resurrect sess123 --project-dir "$GITROOT" --apply >/dev/null
grep -q 'helm work claim' "$tmp/err" || fail "shared-checkout refusal must steer to 'helm work claim'"
if grep -q 'worktree add' "$tmp/err"; then fail "shared-checkout refusal must NOT suggest raw 'git worktree add'"; fi
if grep -vE '^[[:space:]]*#' "$SUT" | grep -q 'worktree add'; then
  fail "helm-session.sh must carry NO raw 'git worktree add' guidance (helm work claim is the lifecycle)"
fi

# 8. resurrect into a real linked worktree: dry-run announces the defuse; --apply pre-creates .remember +
#    emits the CLAUDE_PROJECT_DIR resume line (the working isolation lever, NOT REMEMBER_DIR).
rc="$(rrun resurrect sess123 --project-dir "$WTREE")"
[ "$rc" = 0 ] || fail "resurrect dry-run into a worktree -> exit 0 (got $rc)"
grep -q '^port sess123 .*--to-dir '"$WTREE"'.*--out' "$tmp/cvlog" || fail "resurrect dry-run must cv-port into the worktree with --out"
[ ! -e "$WTREE/.remember" ] || fail "resurrect dry-run must NOT create .remember (apply-only)"
rc="$(rrun resurrect sess123 --project-dir "$WTREE" --apply)"
[ "$rc" = 0 ] || fail "resurrect --apply into a clean worktree -> exit 0 (got $rc)"
[ -d "$WTREE/.remember" ] || fail "resurrect --apply must pre-create .remember (mv-steal defuse)"
grep -q "CLAUDE_PROJECT_DIR='$WTREE'" "$tmp/err" || fail "resurrect --apply must emit the CLAUDE_PROJECT_DIR resume line"

# 9. channel-verify targets the SHARED-BUFFER SIGNATURE, not raw emptiness: it must NOT false-reject a
#    re-resurrect into a permanent home holding the expert's OWN memory, and MUST catch the shared
#    main-checkout buffer copied in — comparing the whole injected set against the main checkout's.
mkdir -p "$GITROOT/.remember"; printf 'shared now-buffer\n' > "$GITROOT/.remember/now.md"   # the shared poison
# 9a. expert's OWN distinct now.md -> apply SUCCEEDS (this is the re-resurrect-into-a-permanent-home case).
printf 'rsh-expert own memory\n' > "$WTREE/.remember/now.md"
rc="$(rrun resurrect sess123 --project-dir "$WTREE" --apply)"
[ "$rc" = 0 ] || fail "resurrect into a home with the expert's OWN now.md -> exit 0, no false-reject (got $rc)"
# 9b. now.md byte-identical to the SHARED main-checkout buffer -> exit 9 (poison detected).
cp "$GITROOT/.remember/now.md" "$WTREE/.remember/now.md"
rc="$(rrun resurrect sess123 --project-dir "$WTREE" --apply)"
[ "$rc" = 9 ] || fail "resurrect with now.md == shared main-checkout buffer -> exit 9 (got $rc)"
grep -qi 'main-checkout buffer' "$tmp/err" || fail "exit-9 must name the shared-buffer poison"
# 9c. the check globs the WHOLE injected set, not just now.md: a today-*.md matching the shared aborts too.
rm -f "$WTREE/.remember/now.md"
printf 'shared today\n' > "$GITROOT/.remember/today-2026-07-03.md"
printf 'shared today\n' > "$WTREE/.remember/today-2026-07-03.md"
rc="$(rrun resurrect sess123 --project-dir "$WTREE" --apply)"
[ "$rc" = 9 ] || fail "resurrect with today-*.md == shared buffer -> exit 9 (globs the injected set) (got $rc)"

# 11. NATIVE-MEMORY re-key verify: resurrect asserts the ported transcript lands in the WORKTREE
#     slug dir (native memory follows the worktree by construction), else fail loud (exit 10).
rm -rf "$PROJ"; mkdir -p "$PROJ"; rm -f "$WTREE/.remember/"*.md 2>/dev/null   # clear 9c poison so we reach the port
# 11a. happy path: apply keys the transcript into the worktree slug dir + announces the re-key.
rc="$(rrun resurrect sess123 --project-dir "$WTREE" --apply)"
[ "$rc" = 0 ] || fail "native-mem: apply into a worktree -> exit 0 (got $rc)"
wslug="$(printf '%s' "$WTREE" | sed 's#[/.]#-#g')"
ls "$PROJ/$wslug"/*.jsonl >/dev/null 2>&1 || fail "native-mem: ported transcript must land in the worktree slug dir"
grep -qi 'native memory re-keyed' "$tmp/err" || fail "native-mem: apply must announce the re-key"
# 11b. mis-keying (transcript lands in the WRONG slug) -> exit 10, fail loud.
BADSTUB="$tmp/cvbad"; cat > "$BADSTUB" <<'BS'
#!/usr/bin/env bash
verb="$1"; shift; out=""
while [ $# -gt 0 ]; do case "$1" in --out) out="$2"; shift 2;; *) shift;; esac; done
case "$verb" in
  show) echo '{"cwd":"/orig/cwd"}' ;;
  port) root="${out:-$HELM_SESSION_PROJECTS_DIR}"; mkdir -p "$root/-wrong-shared-slug"; : > "$root/-wrong-shared-slug/deadbeef.jsonl"; echo "wrote $root/-wrong-shared-slug/deadbeef.jsonl (deadbeef)" ;;
esac
BS
chmod +x "$BADSTUB"
rm -rf "$PROJ"; mkdir -p "$PROJ"; rm -f "$WTREE/.remember/"*.md 2>/dev/null
HELM_SESSION_CV_BIN="$BADSTUB" HELM_SESSION_CVLOG="$tmp/cvlog" HELM_SESSION_SCRATCH="$tmp/rscratch" HELM_SESSION_PROJECTS_DIR="$PROJ" "$SUT" resurrect sess123 --project-dir "$WTREE" --apply >/dev/null 2>"$tmp/err"; rc=$?
[ "$rc" = 10 ] || fail "native-mem: transcript in the wrong slug -> exit 10 (got $rc)"
grep -qi 'did not land in the worktree native-memory slug' "$tmp/err" || fail "exit-10 must name the native-mem re-key failure"
# 11c. FRESH worktree (slug dir does NOT pre-exist) must NOT false-fail — the port creates it, verify passes.
WT2="$tmp/wt-expert2"; git -C "$GITROOT" worktree add -q "$WT2" -b expert-home2
rm -rf "$PROJ"; mkdir -p "$PROJ"
rc="$(rrun resurrect sess123 --project-dir "$WT2" --apply)"
[ "$rc" = 0 ] || fail "native-mem: fresh worktree (no pre-existing slug dir) -> exit 0, no false-fail (got $rc)"

# 10. resurrect missing --project-dir -> exit 2 (mandatory).
[ "$(val resurrect sess123)" = 2 ] || fail "resurrect missing --project-dir -> exit 2"

if [ "$fails" -eq 0 ]; then echo "helm-session guard tests: PASS"; else echo "helm-session guard tests: $fails FAILURE(S)"; fi
exit "$fails"
